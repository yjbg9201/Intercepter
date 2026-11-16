"""GameCore: 盤面・手番・時計・操作モードの唯一の持ち主（I1）。

参照: SPEC §6.1、§6.2、§7（遷移表とレース規則）、§12 の Q1〜Q9（当面の仕様）

骨格：
    run() が self.queue から 1 つ取り出し → 種類ごとのハンドラで状態を更新 →
    _reconcile() でエンジンへの指示を出し直し → スナップショットを配信、を繰り返す。

不変条件との対応：
- 状態はこのクラスだけが持つ。外へ出すのは Snapshot（読み取り専用）だけ（I1）。
- bestmove は search_id が席の expected_search_id と一致するときだけ採用（I5）。奪取で
  expected_search_id をクリアする。hint_search_id の bestmove はコミット経路に入れない（I11）。
- コミットは 1 手番 1 手（I7）。コミットしてから進行役の確認（エコー）が来るまで、次の手番は始まらない。
- UI からの指し手は必ず合法手チェック（I8）。コマンドの ply が現在と違えば拒否。
- 進行役へ指し手を送るのは、その席の手番が始まっていて（_turn_active）未コミットのときだけ。
- 宣言（人間・エンジンとも）は core.nyugyoku で条件成立を確認してからだけ送る。
- 時刻は注入された now() だけを使う。タイマーは TimerFired としてキューに入る。

1 つの席につきエンジンは 1 つ。CSA モード（my_color が決まっている）では LocalSeat は自席だけなので、
エンジンのイベントは色に関係なく自席のものとして扱う。
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import cshogi

from ..engine.base import EngineError, EngineState
from ..engine.usi_parse import build_position
from ..transport.base import Transport
from . import events as ev
from .clock import Clock
from .nyugyoku import DeclarationResult, can_declare, judge_declaration
from .seat import LocalSeat, RemoteSeat, Seat
from .snapshot import ClockView, SeatView, Snapshot
from .types import (
    Actor,
    Color,
    EndReason,
    GameResult,
    Mode,
    MoveRecord,
    NyugyokuRule,
    Phase,
    Score,
    SeatKind,
    TimeControl,
)

SnapshotSink = Callable[[Snapshot], None]
NoticeSink = Callable[[str], None]
InfoSink = Callable[[Color, str, ev.EngineInfoEvent], None]

_SEARCHING = (EngineState.THINKING, EngineState.PONDERING, EngineState.ANALYZING, EngineState.STOPPING)


@dataclass(frozen=True, slots=True)
class GamePolicy:
    """GameCore の振る舞いを決める設定値（config から app.py が組み立てる）。"""

    margin_ms: int = 1000
    time_guard_min_remaining_ms: int = 0  # 0 で無効
    time_guard_action: str = "auto"  # "auto" / "warn"
    on_engine_resign: str = "resign"  # "resign" / "ask"
    on_engine_failure: str = "restart"  # "restart" / "manual"
    nyugyoku_rule: NyugyokuRule = NyugyokuRule.LAW_27
    hint_enabled: bool = False
    hint_multipv: int = 1
    hint_send_as_comment: bool = False  # 人間の手がヒントの候補と一致したら、その評価値・読み筋を付ける
    schedule: tuple[tuple[int, int], ...] = ()  # MANUAL にする通し手数の範囲（v1。SPEC §7.3）


@dataclass(slots=True)
class _SeatRun:
    """LocalSeat 1 つぶんの、エンジンとのやり取りの状態。"""

    expected_search_id: int | None = None  # コミットしうる探索（I5）
    ponder_search_id: int | None = None
    ponder_move: str | None = None  # ponder で仮定している相手の手
    ponder_candidate: str | None = None  # 直前の bestmove が返した予想手（エコー後に ponder を始める）
    hint_search_id: int | None = None  # 表示専用（I11）
    hint_multipv_set: bool = False  # MultiPV をヒント用に変えたままか
    hint_lines: dict[int, tuple[Score, tuple[str, ...]]] = field(default_factory=dict)  # 現局面のヒント（multipv 番号ごと）
    illegal_retries: int = 0  # この手番で非合法 bestmove をやり直した回数
    manual_by_schedule: bool = False


class _NoLog:
    def write(self, kind: str, **fields) -> None:
        pass


class GameCore:
    def __init__(
        self,
        *,
        seats: dict[Color, Seat] | None = None,
        my_seat: LocalSeat | None = None,
        transport: Transport,
        policy: GamePolicy,
        on_snapshot: SnapshotSink | None = None,
        on_notice: NoticeSink | None = None,
        on_engine_info: InfoSink | None = None,
        log=None,  # record.eventlog.EventLog
        now: Callable[[], float] = time.monotonic,
        call_later: Callable | None = None,
        strict: bool = False,
    ) -> None:
        """ローカルモードは seats（両席）を渡す。CSA モードは my_seat（色は仮でよい）を渡し、
        色と相手の席は GameOffered で確定する。"""
        self.queue: asyncio.Queue[ev.Event] = asyncio.Queue()
        self._seats: dict[Color, Seat] = dict(seats or {})
        self._my_seat = my_seat
        self._transport = transport
        self._policy = policy
        self._on_snapshot = on_snapshot or (lambda snapshot: None)
        self._on_notice = on_notice or (lambda text: None)
        self._on_engine_info = on_engine_info or (lambda color, kind, info: None)
        self._log = log if log is not None else _NoLog()
        self._now = now
        self._call_later = call_later
        self._strict = strict

        self._phase = Phase.CONNECTING
        self._board = cshogi.Board()
        self._start_sfen: str | None = None
        self._start_moves: tuple[str, ...] = ()
        self._moves: list[MoveRecord] = []
        self._game_id: str | None = None
        self._my_color: Color | None = None
        self._max_moves: int | None = None
        self._clock: Clock | None = None
        self._runs: dict[Color, _SeatRun] = {c: _SeatRun() for c in Color}
        self._turn_active = False  # 手番が始まっていて、まだコミットしていない
        self._awaiting_echo: Color | None = None  # コミット済みで進行役の確認待ちの席
        self._turn_token = 0
        self._timer = None
        self._hint_enabled = policy.hint_enabled
        self._hint_multipv_on = policy.hint_multipv > 1
        self._result: GameResult | None = None
        self._end_reason: EndReason | None = None
        self._rev = 0
        self._finished = False
        self.notices: list[str] = []

    # ------------------------------------------------------------------ #
    # 外向きの入口
    # ------------------------------------------------------------------ #

    def post(self, event: ev.Event) -> None:
        """イベント・コマンドの唯一の入口。UiGateway / Engine / Transport / タイマーが呼ぶ。"""
        self.queue.put_nowait(event)

    async def run(self) -> None:
        """終局（ENDED）して後片付けが済むまでキューを処理し続ける。"""
        while not self._finished:
            event = await self.queue.get()
            await self._process(event)

    async def process_pending(self) -> None:
        """いまキューにあるイベントを（処理中に入ったものも含めて）すべて処理する。テスト用。"""
        while not self.queue.empty():
            await self._process(self.queue.get_nowait())

    async def _process(self, event: ev.Event) -> None:
        try:
            await self._handle(event)
        except Exception:
            self._log.write("handler_error", event=repr(event), error=traceback.format_exc())
            if self._strict:
                raise
            self._notice("内部エラーが発生しました（ログを確認してください）")
        if not isinstance(event, ev.EngineInfoEvent):
            self._publish()

    # ---- 読み取り専用の参照（app / 棋譜保存用） ----

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def moves(self) -> tuple[MoveRecord, ...]:
        return tuple(self._moves)

    @property
    def result(self) -> GameResult | None:
        return self._result

    @property
    def end_reason(self) -> EndReason | None:
        return self._end_reason

    @property
    def game_id(self) -> str | None:
        return self._game_id

    @property
    def start_sfen(self) -> str | None:
        return self._start_sfen

    @property
    def start_moves(self) -> tuple[str, ...]:
        return self._start_moves

    @property
    def time_controls(self) -> dict[Color, TimeControl] | None:
        return dict(self._clock.tcs) if self._clock else None

    def seat_name(self, color: Color) -> str:
        seat = self._seats.get(color)
        return seat.name if seat else ""

    # ------------------------------------------------------------------ #
    # 小道具
    # ------------------------------------------------------------------ #

    def _notice(self, text: str) -> None:
        self.notices.append(text)
        self._log.write("notice", text=text)
        self._on_notice(text)

    def _reject(self, command: ev.Command, reason: str) -> None:
        self._log.write("command_rejected", command=repr(command), reason=reason)
        self._notice(reason)

    def _publish(self) -> None:
        self._on_snapshot(self.snapshot())

    @property
    def _turn(self) -> Color:
        return Color.BLACK if self._board.turn == cshogi.BLACK else Color.WHITE

    def _usi_moves(self) -> list[str]:
        return [*self._start_moves, *(m.usi for m in self._moves)]

    def _position_cmd(self, extra: tuple[str, ...] = ()) -> str:
        return build_position(self._start_sfen, [*self._usi_moves(), *extra])  # I4: 常に全手順

    def _local_seat(self, color: Color) -> LocalSeat | None:
        seat = self._seats.get(color)
        return seat if isinstance(seat, LocalSeat) else None

    def _local_seats(self) -> list[LocalSeat]:
        return [s for s in self._seats.values() if isinstance(s, LocalSeat)]

    def _engine_seat(self, color: Color) -> LocalSeat | None:
        """エンジンのイベントがどの席のものか。CSA モードは常に自席。"""
        if self._my_color is not None:
            return self._local_seat(self._my_color)
        return self._local_seat(color)

    def _is_legal(self, usi: str) -> bool:
        try:
            move = self._board.move_from_usi(usi)
        except Exception:
            return False
        return bool(move) and self._board.is_legal(move)

    def _own_turn(self, color: Color) -> bool:
        """color の手番が始まっていて、まだコミットしていないか。"""
        return self._phase is Phase.PLAYING and self._turn_active and self._turn is color

    # ------------------------------------------------------------------ #
    # スナップショット
    # ------------------------------------------------------------------ #

    def snapshot(self) -> Snapshot:
        """現在の状態のスナップショット（新しく接続した UI への初回配信用にも使う）。"""
        self._rev += 1
        seats = {}
        for color, seat in self._seats.items():
            if isinstance(seat, LocalSeat):
                seats[color] = SeatView(
                    color=color, kind=SeatKind.LOCAL, name=seat.name, mode=seat.mode,
                    has_engine=seat.engine is not None,
                    engine_state=seat.engine.state.value if seat.engine else None,
                )
            else:
                seats[color] = SeatView(color=color, kind=SeatKind.REMOTE, name=seat.name, mode=None,
                                        has_engine=False, engine_state=None)
        clock_view = None
        if self._clock is not None:
            tc = self._clock.tcs[Color.BLACK]
            clock_view = ClockView(
                remaining_ms=self._clock.remaining_map(), byoyomi_ms=tc.byoyomi_ms,
                increment_ms=tc.increment_ms, running=self._clock.running,
                turn_elapsed_ms=self._clock.turn_elapsed_ms(),
            )
        turn = self._turn
        seat = self._local_seat(turn)
        human_to_move = self._own_turn(turn) and seat is not None and seat.mode is Mode.MANUAL
        legal: tuple[str, ...] = ()
        declare = False
        declare_result: str | None = None
        if human_to_move:
            legal = tuple(cshogi.move_to_usi(m) for m in self._board.legal_moves)
            judged = judge_declaration(self._board, self._policy.nyugyoku_rule)
            declare = judged is not DeclarationResult.NONE
            declare_result = judged.value if declare else None
        return Snapshot(
            rev=self._rev, phase=self._phase, game_id=self._game_id, ply=len(self._moves),
            sfen=self._board.sfen(), turn=turn, my_color=self._my_color, seats=seats, clock=clock_view,
            moves=tuple(self._moves), last_move=self._moves[-1].usi if self._moves else None,
            legal_moves=legal, can_declare=declare, hint_enabled=self._hint_enabled,
            hint_multipv=self._hint_multipv(), hint_multipv_max=max(1, self._policy.hint_multipv),
            start_sfen=self._start_sfen, start_moves=self._start_moves,
            result=self._result, end_reason=self._end_reason, declare_result=declare_result,
        )

    # ------------------------------------------------------------------ #
    # 振り分け
    # ------------------------------------------------------------------ #

    async def _handle(self, event: ev.Event) -> None:
        if isinstance(event, ev.EngineInfoEvent):
            self._on_info(event)
            return
        if isinstance(event, ev.BestMoveEvent):
            await self._on_bestmove(event)
        elif isinstance(event, ev.MoveConfirmed):
            await self._on_move_confirmed(event)
        elif isinstance(event, ev.SetModeCommand):
            await self._on_set_mode(event)
        elif isinstance(event, ev.MoveCommand):
            await self._on_move_command(event)
        elif isinstance(event, ev.ResignCommand):
            await self._on_resign(event)
        elif isinstance(event, ev.DeclareCommand):
            await self._on_declare(event)
        elif isinstance(event, ev.SetHintCommand):
            self._hint_enabled = bool(event.enabled)
            await self._reconcile()
        elif isinstance(event, ev.SetHintMultiPvCommand):
            await self._on_set_hint_multipv(event)
        elif isinstance(event, ev.TimerFired):
            await self._on_timer(event)
        elif isinstance(event, ev.EngineDeadEvent):
            await self._on_engine_dead(event)
        elif isinstance(event, ev.EngineReadyEvent):
            await self._reconcile()
        elif isinstance(event, ev.LoggedIn):
            if self._phase is Phase.CONNECTING:
                self._phase = Phase.WAITING_GAME
        elif isinstance(event, ev.GameOffered):
            await self._on_game_offered(event)
        elif isinstance(event, ev.GameStarted):
            await self._on_game_started(event)
        elif isinstance(event, ev.GameRejected):
            self._phase = Phase.WAITING_GAME
            self._notice(f"対局が成立しませんでした（{event.by or '相手'} が拒否）。次のマッチを待ちます")
        elif isinstance(event, ev.GameEnded):
            await self._on_game_ended(event.result, event.reason, event.detail)
        elif isinstance(event, ev.TransportClosed):
            if self._phase is not Phase.ENDED:
                self._notice(f"サーバとの接続が切れました {event.detail}".strip())
                await self._on_game_ended(GameResult.ABORTED, EndReason.DISCONNECTED, event.detail)
        else:
            self._log.write("unknown_event", event=repr(event))

    # ------------------------------------------------------------------ #
    # 対局の開始・終了
    # ------------------------------------------------------------------ #

    async def _on_game_offered(self, e: ev.GameOffered) -> None:
        if self._phase in (Phase.PLAYING, Phase.ENDED):
            return
        self._game_id = e.game_id
        self._my_color = e.my_color
        self._max_moves = e.max_moves
        self._start_sfen = e.start_sfen
        self._start_moves = tuple(e.start_moves)
        self._board = cshogi.Board(sfen=e.start_sfen) if e.start_sfen else cshogi.Board()
        for usi in e.start_moves:
            self._board.push_usi(usi)
        self._moves = []
        self._runs = {c: _SeatRun() for c in Color}
        self._clock = Clock(
            {Color.BLACK: e.time_control, Color.WHITE: e.time_control_white or e.time_control},
            now=self._now,
        )
        names = {Color.BLACK: e.black_name, Color.WHITE: e.white_name}
        if e.my_color is not None:
            if self._my_seat is None:
                raise RuntimeError("CSA モードには my_seat が必要です")
            mine = self._my_seat
            mine.color = e.my_color
            mine.name = names[e.my_color] or mine.name
            self._seats = {
                e.my_color: mine,
                e.my_color.opponent: RemoteSeat(color=e.my_color.opponent, name=names[e.my_color.opponent]),
            }
        self._phase = Phase.READY
        guard = self._policy.time_guard_min_remaining_ms
        per_move = e.time_control.byoyomi_ms + e.time_control.increment_ms
        if guard and self._policy.time_guard_action == "auto" and guard >= per_move:
            # Q5: 閾値の自動調整はしない。警告だけ出す
            self._notice(
                f"TimeGuard の閾値（{guard // 1000} 秒）が 1 手あたりの秒読み・加算（{per_move // 1000} 秒）以上です。"
                "持ち時間が減ると MANUAL が使えなくなります"
            )
        self._log.write("game_offered", game_id=e.game_id, black=e.black_name, white=e.white_name,
                        my_color=e.my_color, start_sfen=e.start_sfen, start_moves=list(e.start_moves))
        for seat in self._local_seats():
            if seat.engine is not None:
                await seat.engine.new_game()
        await self._transport.agree()

    async def _on_game_started(self, e: ev.GameStarted) -> None:
        if self._phase is not Phase.READY:
            return
        self._phase = Phase.PLAYING
        self._log.write("game_started", game_id=e.game_id)
        await self._begin_turn(self._turn)

    async def _on_game_ended(self, result: GameResult, reason: EndReason, detail: str = "") -> None:
        if self._phase is Phase.ENDED:
            return
        self._phase = Phase.ENDED
        self._result, self._end_reason = result, reason
        self._turn_active = False
        self._awaiting_echo = None
        self._cancel_timer()
        if self._clock is not None:
            self._clock.stop()
        self._log.write("game_ended", result=result, reason=reason, detail=detail)
        for seat in self._local_seats():
            run = self._runs[seat.color]
            run.expected_search_id = run.ponder_search_id = run.hint_search_id = None
            if seat.engine is None:
                continue
            await seat.engine.cancel()
            if run.hint_multipv_set:
                # ヒント用に変えた MultiPV を戻す。エンジンは次の対局でも使い回すので、残すと次局を MultiPV > 1 で指す
                try:
                    await seat.engine.set_options({"MultiPV": 1})
                    run.hint_multipv_set = False
                except Exception as exc:  # 終局処理は止めない（エンジンが死んでいる場合など）
                    self._log.write("multipv_restore_failed", color=seat.color, error=repr(exc))
            if result is GameResult.DRAW or result is GameResult.ABORTED:
                outcome = "draw"
            else:
                won = (result is GameResult.BLACK_WIN) == (seat.color is Color.BLACK)
                outcome = "win" if won else "lose"
            await seat.engine.game_over(outcome)
        self._finished = True

    # ------------------------------------------------------------------ #
    # 手番の開始
    # ------------------------------------------------------------------ #

    async def _begin_turn(self, color: Color, last_move: str | None = None) -> None:
        """color の手番を始める。直前の手（last_move）が確定した直後に呼ぶ。"""
        assert self._clock is not None
        self._turn_active = True
        self._turn_token += 1
        self._cancel_timer()
        self._clock.start_turn(color)
        seat = self._local_seat(color)
        if seat is not None:
            run = self._runs[color]
            run.illegal_retries = 0
            self._apply_schedule(seat, run)
            # ponder の決着: 予想手が当たっていて AUTO のままなら ponderhit、外れなら cancel
            if run.ponder_search_id is not None and seat.engine is not None:
                hit = (seat.mode is Mode.AUTO and last_move is not None and last_move == run.ponder_move
                       and seat.engine.state is EngineState.PONDERING)
                if hit:
                    await seat.engine.ponderhit()
                    run.expected_search_id = run.ponder_search_id
                    self._log.write("ponderhit", color=color, search_id=run.ponder_search_id)
                else:
                    await seat.engine.cancel()
                    self._log.write("ponder_miss", color=color, search_id=run.ponder_search_id,
                                    expected=run.ponder_move, actual=last_move)
                run.ponder_search_id = None
                run.ponder_move = None
        await self._reconcile()
        self._arm_time_guard()

    def _apply_schedule(self, seat: LocalSeat, run: _SeatRun) -> None:
        """予約切替（v1。SPEC §7.3）。スケジュールが MANUAL にした場合に限り、範囲を抜けたら戻す。"""
        if not self._policy.schedule:
            return
        ply = len(self._start_moves) + len(self._moves) + 1
        in_range = any(first <= ply <= last for first, last in self._policy.schedule)
        if in_range and seat.mode is Mode.AUTO:
            self._set_mode(seat, Mode.MANUAL, by="schedule")
            run.manual_by_schedule = True
        elif not in_range and run.manual_by_schedule:
            run.manual_by_schedule = False
            if seat.mode is Mode.MANUAL and seat.engine is not None:
                self._set_mode(seat, Mode.AUTO, by="schedule")

    def _set_mode(self, seat: LocalSeat, mode: Mode, *, by: str) -> None:
        if seat.mode is mode:
            return
        seat.mode = mode
        run = self._runs[seat.color]
        if mode is Mode.MANUAL:
            # 以後に届く bestmove は search_id 不一致で破棄される（SPEC §7.2）
            run.expected_search_id = None
        self._log.write("mode_change", color=seat.color, mode=mode, by=by)

    # ------------------------------------------------------------------ #
    # エンジンへの指示（1 か所に集める。SPEC §6.1）
    # ------------------------------------------------------------------ #

    async def _reconcile(self) -> None:
        """各 LocalSeat のエンジンが「今しているべきこと」と現状の差分を埋める。

        §7.1 の遷移表の「処理」列のうち、エンジンに関する部分はすべてここを通る。
        """
        for seat in self._local_seats():
            await self._reconcile_seat(seat)

    def _hint_multipv(self) -> int:
        """いま有効なヒントの MultiPV。"""
        return max(1, self._policy.hint_multipv) if self._hint_multipv_on else 1

    async def _on_set_hint_multipv(self, c: ev.SetHintMultiPvCommand) -> None:
        on = bool(c.enabled) and self._policy.hint_multipv > 1
        if on == self._hint_multipv_on:
            return
        self._hint_multipv_on = on
        # MultiPV は IDLE でしか変えられないので、動いているヒント探索は止めてからやり直す
        for seat in self._local_seats():
            run = self._runs[seat.color]
            if seat.engine is not None and run.hint_search_id is not None:
                self._log.write("search_abandon", color=seat.color, have="hint", want="hint")
                run.hint_search_id = None
                await seat.engine.cancel()
        await self._reconcile()

    def _desired(self, seat: LocalSeat) -> str:
        if self._phase is not Phase.PLAYING:
            return "none"
        if self._own_turn(seat.color):
            if seat.mode is Mode.AUTO:
                return "think"
            return "hint" if self._hint_enabled else "none"
        # 相手の手番（または確認待ち）: AUTO なら動いている ponder を続ける。新しくは始めない
        return "ponder" if seat.mode is Mode.AUTO else "none"

    async def _reconcile_seat(self, seat: LocalSeat) -> None:
        engine = seat.engine
        if engine is None:
            return
        run = self._runs[seat.color]
        want = self._desired(seat)
        if run.expected_search_id is not None:
            have = "think"
        elif run.ponder_search_id is not None:
            have = "ponder"
        elif run.hint_search_id is not None:
            have = "hint"
        elif engine.state in _SEARCHING:
            # 奪取などで search_id を先に捨てた探索がまだ動いている。必ず止める
            have = "orphan"
        else:
            have = "none"
        if have == want:
            return

        if have != "none":
            self._log.write("search_abandon", color=seat.color, have=have, want=want)
            run.expected_search_id = run.ponder_search_id = run.hint_search_id = None
            run.ponder_move = None
            await engine.cancel()
        if want in ("none", "ponder"):
            return  # ponder はエコーの後で _start_ponder が始める
        if engine.state is not EngineState.IDLE:
            # 再起動中など。EngineReadyEvent でもう一度ここに来る
            if engine.state is EngineState.DEAD and want == "think":
                self._fall_back_to_manual(seat, "エンジンが動いていないため MANUAL に切り替えました")
            return
        if not self._own_turn(seat.color) or self._desired(seat) != want:
            return  # cancel を待つ間に状況が変わった

        assert self._clock is not None
        try:
            if want == "think":
                if run.hint_multipv_set:
                    await engine.set_options({"MultiPV": 1})
                    run.hint_multipv_set = False
                go_time = self._clock.go_time(seat.color, margin_ms=self._policy.margin_ms)
                run.expected_search_id = await engine.think(self._position_cmd(), go_time)
                self._log.write("think", color=seat.color, search_id=run.expected_search_id,
                                ply=len(self._moves) + 1, go_time=repr(go_time))
            else:
                multipv = self._hint_multipv()
                if multipv > 1 and not run.hint_multipv_set:
                    await engine.set_options({"MultiPV": multipv})
                    run.hint_multipv_set = True
                elif multipv == 1 and run.hint_multipv_set:
                    await engine.set_options({"MultiPV": 1})
                    run.hint_multipv_set = False
                run.hint_lines.clear()
                run.hint_search_id = await engine.analyze(self._position_cmd())
                self._log.write("hint", color=seat.color, search_id=run.hint_search_id)
        except EngineError as exc:
            self._log.write("engine_error", color=seat.color, error=repr(exc))
            if self._strict:
                raise

    async def _start_ponder(self, seat: LocalSeat) -> None:
        """自分の手が確定した直後に、返ってきた予想手で ponder を始める（Q6: エコーの後）。"""
        run = self._runs[seat.color]
        candidate, run.ponder_candidate = run.ponder_candidate, None
        engine = seat.engine
        if (candidate is None or engine is None or not seat.ponder_enabled or seat.mode is not Mode.AUTO
                or self._phase is not Phase.PLAYING or engine.state is not EngineState.IDLE
                or not self._is_legal(candidate)):
            return
        assert self._clock is not None
        go_time = self._clock.go_time(seat.color, margin_ms=self._policy.margin_ms, pondering=True)
        try:
            run.ponder_search_id = await engine.ponder(self._position_cmd((candidate,)), go_time)
        except EngineError as exc:
            self._log.write("engine_error", color=seat.color, error=repr(exc))
            return
        run.ponder_move = candidate
        self._log.write("ponder", color=seat.color, search_id=run.ponder_search_id, move=candidate)

    def _fall_back_to_manual(self, seat: LocalSeat, text: str) -> None:
        if seat.mode is Mode.MANUAL:
            return
        self._set_mode(seat, Mode.MANUAL, by="engine_failure")
        self._runs[seat.color].manual_by_schedule = False
        self._notice(text)
        self._arm_time_guard()

    # ------------------------------------------------------------------ #
    # エンジンのイベント
    # ------------------------------------------------------------------ #

    def _on_info(self, e: ev.EngineInfoEvent) -> None:
        seat = self._engine_seat(e.color)
        if seat is None:
            return
        run = self._runs[seat.color]
        if e.search_id == run.hint_search_id:
            kind = "hint"
            if e.pv and e.score is not None and e.score.bound is None:
                run.hint_lines[e.multipv] = (e.score, tuple(e.pv))  # 暫定値（lower / upper）は残さない
        elif e.search_id == run.expected_search_id:
            kind = "think"
        elif e.search_id == run.ponder_search_id:
            kind = "ponder"
        else:
            return
        self._on_engine_info(seat.color, kind, e)

    async def _on_bestmove(self, e: ev.BestMoveEvent) -> None:
        seat = self._engine_seat(e.color)
        if seat is None:
            return
        color = seat.color
        run = self._runs[color]
        if e.search_id == run.hint_search_id:
            # ヒント探索の結果は絶対にコミットしない（I11）
            run.hint_search_id = None
            self._log.write("bestmove_discarded", color=color, search_id=e.search_id, move=e.move, reason="hint")
            await self._reconcile()
            return
        if run.expected_search_id is None or e.search_id != run.expected_search_id:
            self._log.write("bestmove_discarded", color=color, search_id=e.search_id, move=e.move,
                            reason="search_id mismatch", expected=run.expected_search_id)
            return
        run.expected_search_id = None
        if not self._own_turn(color) or seat.mode is not Mode.AUTO:
            self._log.write("bestmove_discarded", color=color, search_id=e.search_id, move=e.move,
                            reason="not committable")
            return

        if e.move == "resign":
            if self._policy.on_engine_resign == "ask":
                self._set_mode(seat, Mode.MANUAL, by="engine_resign")
                self._notice("エンジンが投了しようとしています。投了するか、自分で指し継ぐかを選んでください")
                await self._reconcile()
                self._arm_time_guard()
                return
            await self._commit_special(color, "resign", Actor.ENGINE)
            return
        if e.move == "win":
            if can_declare(self._board, self._policy.nyugyoku_rule):
                await self._commit_special(color, "declare", Actor.ENGINE)
            else:
                # 条件を満たさない宣言は送らない（Q8）。非合法な bestmove と同じ扱い
                await self._on_bad_bestmove(seat, "エンジンが条件を満たさない入玉宣言（bestmove win）を返しました")
            return
        if not self._is_legal(e.move):
            await self._on_bad_bestmove(seat, f"エンジンが非合法な手（{e.move}）を返しました")
            return

        await self._commit_move(color, e.move, Actor.ENGINE, score=e.score, pv=e.pv)
        run.ponder_candidate = e.ponder if seat.ponder_enabled else None

    async def _on_bad_bestmove(self, seat: LocalSeat, text: str) -> None:
        run = self._runs[seat.color]
        self._log.write("engine_bad_bestmove", color=seat.color, text=text)
        if self._policy.on_engine_failure == "restart" and run.illegal_retries < 1:
            run.illegal_retries += 1
            self._notice(text + "。考え直させます")
            await self._reconcile()
            return
        self._fall_back_to_manual(seat, text + "。MANUAL に切り替えました")
        await self._reconcile()

    async def _on_engine_dead(self, e: ev.EngineDeadEvent) -> None:
        seat = self._engine_seat(e.color)
        if seat is None:
            return
        run = self._runs[seat.color]
        run.expected_search_id = run.ponder_search_id = run.hint_search_id = None
        run.ponder_move = None
        run.hint_multipv_set = False
        self._log.write("engine_dead_seen", color=seat.color, reason=e.reason, will_restart=e.will_restart)
        if self._phase is not Phase.PLAYING:
            return
        if seat.mode is Mode.AUTO:
            if self._policy.on_engine_failure == "manual":
                self._fall_back_to_manual(seat, f"エンジン異常（{e.reason}）のため MANUAL に切り替えました")
            elif not e.will_restart:
                self._fall_back_to_manual(seat, f"エンジンを再起動できません（{e.reason}）。MANUAL に切り替えました")
            else:
                self._notice(f"エンジン異常（{e.reason}）。再起動しています")
        else:
            self._notice(f"エンジン異常（{e.reason}）" + ("。再起動しています" if e.will_restart else ""))
        await self._reconcile()

    # ------------------------------------------------------------------ #
    # コミット
    # ------------------------------------------------------------------ #

    async def _commit_move(
        self, color: Color, usi: str, actor: Actor, *, score: Score | None = None, pv: tuple[str, ...] = ()
    ) -> None:
        """指し手を確定して進行役へ送る。呼び出し側が手番・合法性を確認済みであること（I7, I8）。"""
        assert self._own_turn(color)
        self._turn_active = False
        self._awaiting_echo = color
        self._cancel_timer()
        ply = len(self._moves) + 1
        self._runs[color].hint_lines.clear()  # 次の局面に持ち越さない
        self._board.push_usi(usi)
        self._moves.append(MoveRecord(ply=ply, usi=usi, color=color, actor=actor, score=score, pv=tuple(pv)))
        self._log.write("commit", color=color, ply=ply, usi=usi, actor=actor)
        if score is not None:
            # エンジンの手。人間の手は hint_send_as_comment のときだけ score が付いて来る（_on_move_command）
            await self._transport.send_move(color, usi, score=score, pv=tuple(pv))
        else:
            await self._transport.send_move(color, usi)  # 人間の手には既定でコメントを付けない

    async def _commit_special(self, color: Color, kind: str, actor: Actor) -> None:
        """投了・宣言。これも 1 手番 1 回のコミット。"""
        assert self._own_turn(color)
        self._turn_active = False
        self._awaiting_echo = color
        self._cancel_timer()
        self._log.write("commit", color=color, special=kind, actor=actor)
        if kind == "resign":
            await self._transport.send_resign(color)
        else:
            await self._transport.send_declare(color)

    async def _on_move_confirmed(self, e: ev.MoveConfirmed) -> None:
        if self._phase is not Phase.PLAYING:
            return
        assert self._clock is not None
        if self._awaiting_echo is e.color:
            # 自分（LocalSeat）の手のエコー。検証と時計の確定
            self._awaiting_echo = None
            record = self._moves[-1]
            if record.usi != e.usi:
                self._log.write("echo_mismatch", sent=record.usi, received=e.usi)
                self._notice(f"進行役の確認が送った手と違います（送信 {record.usi} / 確認 {e.usi}）")
            self._moves[-1] = replace(record, elapsed_ms=e.elapsed_ms)
        else:
            # 相手（RemoteSeat）の手
            if self._awaiting_echo is not None or self._turn is not e.color or isinstance(
                    self._seats.get(e.color), LocalSeat):
                self._log.write("unexpected_move", color=e.color, usi=e.usi)
                return
            if not self._is_legal(e.usi):
                self._log.write("remote_illegal_move", color=e.color, usi=e.usi)
                self._notice(f"相手の手 {e.usi} が非合法です（進行役の判定を待ちます）")
                return
            ply = len(self._moves) + 1
            self._board.push_usi(e.usi)
            self._moves.append(MoveRecord(ply=ply, usi=e.usi, color=e.color, actor=Actor.REMOTE,
                                          elapsed_ms=e.elapsed_ms, score=e.score, pv=tuple(e.pv)))
            self._turn_active = False
        self._clock.apply_move(e.color, e.elapsed_ms)
        self._log.write("move_confirmed", color=e.color, usi=e.usi, elapsed_ms=e.elapsed_ms,
                        ply=len(self._moves))
        await self._begin_turn(self._turn, last_move=e.usi)
        mover = self._local_seat(e.color)
        if mover is not None:
            await self._start_ponder(mover)

    # ------------------------------------------------------------------ #
    # UI コマンド
    # ------------------------------------------------------------------ #

    async def _on_set_mode(self, c: ev.SetModeCommand) -> None:
        seat = self._local_seat(c.color)
        if seat is None:
            self._reject(c, "その席は操作できません（相手の席です）")
            return
        if self._phase is Phase.ENDED:
            self._reject(c, "対局は終了しています")
            return
        if seat.mode is c.mode:
            return
        run = self._runs[seat.color]
        if c.mode is Mode.MANUAL:
            if self._time_guard_blocks(seat.color):
                self._reject(c, "残り時間が少ないため奪取できません（TimeGuard）")
                return
            self._set_mode(seat, Mode.MANUAL, by=c.client_id or "ui")
            run.manual_by_schedule = False
            if self._phase is Phase.PLAYING and not self._own_turn(seat.color):
                if self._awaiting_echo is seat.color or (self._moves and self._moves[-1].color is seat.color):
                    self._notice("送信済みのため、奪取は次の手番から有効です")
            await self._reconcile()
            self._arm_time_guard()
        else:
            if seat.engine is None:
                self._reject(c, "この席にはエンジンがないので返却できません")
                return
            if seat.engine.state is EngineState.DEAD:
                self._reject(c, "エンジンが動いていないので返却できません")
                return
            self._set_mode(seat, Mode.AUTO, by=c.client_id or "ui")
            run.manual_by_schedule = False
            self._cancel_timer()
            await self._reconcile()

    def _check_turn_command(self, c, color: Color, ply: int) -> LocalSeat | None:
        """着手・投了・宣言に共通の検査。通れば席を返す。"""
        if self._phase is not Phase.PLAYING:
            self._reject(c, "対局中ではありません")
            return None
        seat = self._local_seat(color)
        if seat is None:
            self._reject(c, "その席は操作できません（相手の席です）")
            return None
        if ply != len(self._moves):
            self._reject(c, "古い画面からの操作のため無視しました（局面が進んでいます）")
            return None
        if not self._own_turn(color):
            self._reject(c, "手番ではありません（または既に着手済みです）")
            return None
        if seat.mode is not Mode.MANUAL:
            self._reject(c, "AUTO の席です。先に奪取してください")
            return None
        return seat

    async def _on_move_command(self, c: ev.MoveCommand) -> None:
        seat = self._check_turn_command(c, c.color, c.ply)
        if seat is None:
            return
        if not self._is_legal(c.usi):
            self._reject(c, f"非合法な手です: {c.usi}")
            return
        score, pv = self._hint_line_for(c.color, c.usi)
        await self._commit_move(c.color, c.usi, Actor.HUMAN, score=score, pv=pv)
        await self._reconcile()  # ヒント探索中なら cancel

    def _hint_line_for(self, color: Color, usi: str) -> tuple[Score | None, tuple[str, ...]]:
        """人間の手に付ける評価値・読み筋。設定がオンで、現局面のヒントの候補と一致したときだけ返す。"""
        if not self._policy.hint_send_as_comment:
            return None, ()
        lines = self._runs[color].hint_lines
        for multipv in sorted(lines):
            score, pv = lines[multipv]
            if pv[:1] == (usi,):
                return score, pv
        return None, ()

    async def _on_resign(self, c: ev.ResignCommand) -> None:
        if self._check_turn_command(c, c.color, c.ply) is None:
            return
        await self._commit_special(c.color, "resign", Actor.HUMAN)
        await self._reconcile()

    async def _on_declare(self, c: ev.DeclareCommand) -> None:
        if self._check_turn_command(c, c.color, c.ply) is None:
            return
        if not can_declare(self._board, self._policy.nyugyoku_rule):
            self._reject(c, "入玉宣言の条件を満たしていません（宣言は送りません）")
            return
        await self._commit_special(c.color, "declare", Actor.HUMAN)
        await self._reconcile()

    # ------------------------------------------------------------------ #
    # TimeGuard（SPEC §7.4、Q4, Q5）
    # ------------------------------------------------------------------ #

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _time_guard_blocks(self, color: Color) -> bool:
        """action=auto のとき、閾値未満の間は奪取させない（Q4）。"""
        p = self._policy
        if not p.time_guard_min_remaining_ms or p.time_guard_action != "auto":
            return False
        if self._phase is not Phase.PLAYING or self._clock is None:
            return False
        seat = self._local_seat(color)
        if seat is None or seat.engine is None or seat.engine.state is EngineState.DEAD:
            return False  # 戻す先のエンジンがなければ止めても意味がない
        return self._clock.manual_budget_ms(color) < p.time_guard_min_remaining_ms

    def _arm_time_guard(self) -> None:
        """MANUAL の自手番なら、閾値に達する時刻にタイマーを張る。"""
        self._cancel_timer()
        threshold = self._policy.time_guard_min_remaining_ms
        if not threshold or self._phase is not Phase.PLAYING or self._clock is None or not self._turn_active:
            return
        seat = self._local_seat(self._turn)
        if seat is None or seat.mode is not Mode.MANUAL:
            return
        delay_ms = max(0, self._clock.manual_budget_ms(seat.color) - threshold + 1)
        token = self._turn_token
        call_later = self._call_later or asyncio.get_running_loop().call_later
        self._timer = call_later(delay_ms / 1000, self.post, ev.TimerFired(kind="time_guard", turn_token=token))

    async def _on_timer(self, e: ev.TimerFired) -> None:
        if e.kind != "time_guard" or e.turn_token != self._turn_token or not self._turn_active:
            return  # 古い手番のタイマー
        if self._phase is not Phase.PLAYING or self._clock is None:
            return
        seat = self._local_seat(self._turn)
        if seat is None or seat.mode is not Mode.MANUAL:
            return
        threshold = self._policy.time_guard_min_remaining_ms
        if self._clock.manual_budget_ms(seat.color) >= threshold:
            self._arm_time_guard()  # まだ早い（張り直す）
            return
        self._log.write("timer", timer="time_guard", color=seat.color)
        usable = seat.engine is not None and seat.engine.state is not EngineState.DEAD
        if self._policy.time_guard_action == "auto" and usable:
            self._set_mode(seat, Mode.AUTO, by="time_guard")
            self._runs[seat.color].manual_by_schedule = False
            self._notice("残り時間が少ないため、TimeGuard で AUTO に戻しました")
            await self._reconcile()
        else:
            self._notice("残り時間が少なくなっています（TimeGuard）")
