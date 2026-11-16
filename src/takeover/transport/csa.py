"""CsaTransport: CSA サーバ（Floodgate / shogi-server）のクライアント（SPEC §6.5）。

- 受信は専用タスク 1 つ。行の解析は csa_parse の純関数。
- 既定では LOGIN に x1 を付けない（付けると自動マッチ登録が効かない）。
- 自分の手番でないとき、または送った手の確認（エコー）待ちのときに指し手を送ろうとしたら、
  送らずに TransportError（サーバは手番外の指し手を保留して次の手番に適用してしまうため）。
- 人間の手（score なしで呼ばれた send_move）にはコメントを付けない。
- 盤面はサーバの確認（`<指し手>,T<n>`）でだけ進める。自分の手のエコーは指し手部分の一致を確認する。
- 終局は「理由行 → 結果行」。結果行で GameEnded を emit。
- キープアライブ: 最後の送信から keepalive_sec 経ったら空行を送る。待機中も送る。
- 切断は TransportClosed を emit（自動再接続しない）。
- 送受信した生の行をイベントログへ。LOGIN 行は csa_parse.mask_login で伏せる（I10）。
- on_line(方向, 行) があれば、送受信した行（LOGIN は伏せたもの）を渡す。UI の通信ログ用（SPEC §6.11）。
- `##` で始まる行は拡張コマンドの応答。終局の理由行（`#RESIGN` など）として扱わない。
- send_raw() は対局の進行に関わらない行（`%%WHO` など）だけを送る。指し手・特殊な指し手は送らせない。
- send_special() は `%HIKIWAKE` など任意の `%X` を、投了・宣言と同じ検査（自分の手番・確認待ちなし）で送る。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import cshogi

from ..core.events import (
    GameEnded,
    GameOffered,
    GameRejected,
    GameStarted,
    LoggedIn,
    MoveConfirmed,
    TransportClosed,
)
from ..core.types import Color, EndReason, GameResult, Score
from . import csa_parse
from .base import TransportEmitFn


class TransportError(Exception):
    """送ってはいけない状況で送ろうとした（呼び出し側のバグ）。何も送っていない。"""


@dataclass(frozen=True, slots=True)
class CsaSettings:
    host: str
    port: int
    login_name: str
    login_password: str  # 秘密。ログ・スナップショットに出さない
    x1: bool = False
    send_engine_comment: bool = True
    comment_score_pov: str = "black"  # "black"（先手視点。Floodgate の慣例）/ "mover"
    keepalive_sec: float = 60
    connect_timeout_ms: int = 10000
    agree_timeout_ms: int = 30000


class _NoLog:
    def write(self, kind: str, **fields) -> None:
        pass


class CsaTransport:
    def __init__(
        self,
        settings: CsaSettings,
        *,
        emit: TransportEmitFn,
        log=None,
        now: Callable[[], float] = time.monotonic,
        on_line: Callable[[str, str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._on_line_hook = on_line
        self._log = log if log is not None else _NoLog()
        self._now = now
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []
        self._last_send = 0.0
        self._closing = False
        self._broken = False  # 送信に失敗した（接続が切れている）
        self._closed_emitted = False
        self._summary: csa_parse.GameSummary | None = None
        self._summary_lines: list[str] | None = None
        self._board = cshogi.Board()
        self._started = False
        self._ended = False
        self._pending: str | None = None  # 送ってエコー待ちの行の先頭部分（"+7776FU" / "%TORYO"）
        self._reason: EndReason | None = None
        self._reason_raw = ""

    @property
    def summary(self) -> csa_parse.GameSummary | None:
        return self._summary

    # ------------------------------------------------------------------ #
    # 接続
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        s = self._settings
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(s.host, s.port), s.connect_timeout_ms / 1000)
        except (OSError, asyncio.TimeoutError) as exc:
            self._log.write("csa_connect_failed", host=s.host, port=s.port, error=repr(exc))
            self._emit_closed(f"{s.host}:{s.port} に接続できません（{exc}）")
            return
        self._log.write("csa_connected", host=s.host, port=s.port)
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._keepalive_loop())]
        await self._send(csa_parse.build_login(s.login_name, s.login_password, x1=s.x1))

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._writer is not None:
            try:
                await self._send("LOGOUT")
            except Exception:
                pass
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 2)
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    def _emit_closed(self, detail: str) -> None:
        if self._closing or self._closed_emitted:
            return
        self._closed_emitted = True
        self._emit(TransportClosed(detail=detail))

    async def _send(self, line: str) -> None:
        if self._writer is None:
            raise TransportError("接続していません")
        self._log.write("csa_send", line=csa_parse.mask_login(line))
        self._notify_line("send", csa_parse.mask_login(line))
        self._last_send = self._now()
        try:
            self._writer.write((line + "\n").encode("utf-8"))
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            self._log.write("csa_send_failed", error=repr(exc))
            self._broken = True
            self._emit_closed(f"送信に失敗しました（{exc}）")

    def _notify_line(self, direction: str, line: str) -> None:
        if self._on_line_hook is not None:
            try:
                self._on_line_hook(direction, line)
            except Exception:
                pass  # 表示用の通知で通信を止めない

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._closing and not self._broken

    async def _keepalive_loop(self) -> None:
        interval = self._settings.keepalive_sec
        if interval <= 0:
            return
        while not self._broken:  # 切れた接続へ送り続けない
            wait = self._last_send + interval - self._now()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            await self._send("")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._log.write("csa_recv", line=line)
                self._notify_line("recv", line)
                try:
                    self._on_line(line)
                except Exception as exc:  # 1 行の解析失敗で受信を止めない
                    self._log.write("csa_line_error", line=line, error=repr(exc))
        except (ConnectionError, OSError) as exc:
            self._log.write("csa_recv_failed", error=repr(exc))
        self._emit_closed("サーバが接続を閉じました")

    # ------------------------------------------------------------------ #
    # 受信行の処理
    # ------------------------------------------------------------------ #

    def _on_line(self, line: str) -> None:
        if line == "":
            return  # キープアライブの応答
        if self._summary_lines is not None:
            self._summary_lines.append(line)
            if line == "END Game_Summary":
                lines, self._summary_lines = self._summary_lines, None
                self._on_summary(lines)
            return
        if line == "BEGIN Game_Summary":
            self._summary_lines = [line]
            return
        if line.startswith("LOGIN:"):
            if line.endswith(" OK"):
                self._log.write("csa_logged_in")
                self._emit(LoggedIn())
            else:
                self._emit_closed("LOGIN に失敗しました（名前かパスワードが違います）")
            return
        if line.startswith("START:"):
            self._started = True
            self._emit(GameStarted(game_id=line[6:]))
            return
        if line.startswith("REJECT:"):
            rest = line[7:]
            game_id, _, by = rest.partition(" by ")
            self._summary = None
            self._emit(GameRejected(game_id=game_id, by=by))
            return
        if line.startswith("##"):
            return  # 拡張コマンドの応答（##[WHO] など）。ログと通信欄に出すだけ
        if line.startswith("#"):
            self._on_end_line(line)
            return
        move = csa_parse.parse_server_move(line)
        if move is not None:
            self._on_server_move(move)

    def _on_summary(self, lines: list[str]) -> None:
        summary = csa_parse.parse_game_summary(lines)
        self._summary = summary
        self._board = cshogi.Board(sfen=summary.start_sfen) if summary.start_sfen else cshogi.Board()
        for usi in summary.start_moves:
            self._board.push_usi(usi)
        self._started = self._ended = False
        self._pending = None
        self._reason = None
        if summary.unknown:
            self._log.write("csa_summary_unknown_keys", keys=summary.unknown)
        self._emit(GameOffered(
            game_id=summary.game_id, black_name=summary.black_name, white_name=summary.white_name,
            my_color=summary.my_color, time_control=summary.time_control[Color.BLACK],
            time_control_white=summary.time_control[Color.WHITE],
            start_sfen=summary.start_sfen, start_moves=summary.start_moves, max_moves=summary.max_moves,
        ))

    def _turn(self) -> Color:
        return Color.BLACK if self._board.turn == cshogi.BLACK else Color.WHITE

    def _on_server_move(self, move: csa_parse.ServerMove) -> None:
        if self._summary is None or not self._started or self._ended:
            return
        if move.csa.startswith("%"):
            if self._pending == move.csa:
                self._pending = None
            return  # 投了・宣言のエコー。続く `#` 行で終局する
        mover = move.color
        if mover is not self._turn():
            self._log.write("csa_move_out_of_turn", line=move.csa)
            return
        if mover is self._summary.my_color:
            if self._pending != move.csa:
                self._log.write("csa_echo_mismatch", sent=self._pending, received=move.csa)
            self._pending = None
        usi = csa_parse.csa_to_usi(self._board, move.csa)
        self._board.push_usi(usi)
        unit = self._summary.time_control[mover].time_unit_ms
        score = None
        pv: tuple[str, ...] = ()
        if move.comment:
            parsed = csa_parse.parse_comment(move.comment)
            if parsed is not None:
                value = csa_parse.comment_to_mover_score(parsed[0], mover, self._settings.comment_score_pov)
                score = Score(cp=value)
                pv = parsed[1]
        self._emit(MoveConfirmed(color=mover, usi=usi, elapsed_ms=(move.elapsed_units or 0) * unit,
                                 score=score, pv=pv))

    def _on_end_line(self, line: str) -> None:
        end = csa_parse.parse_end_line(line)
        if end is None or self._summary is None:
            return
        if end.reason is not None:
            self._reason, self._reason_raw = end.reason, end.raw
            return
        if self._ended:
            return
        self._ended = True
        mine = self._summary.my_color
        reason = self._reason or EndReason.UNKNOWN

        def win(color: Color) -> GameResult:
            return GameResult.BLACK_WIN if color is Color.BLACK else GameResult.WHITE_WIN

        if end.result == "win":
            result = win(mine)
        elif end.result == "lose":
            result = win(mine.opponent)
        elif end.result == "draw":
            result = GameResult.DRAW
        elif end.result == "censored":
            result = GameResult.DRAW if reason is EndReason.MAX_MOVES else GameResult.ABORTED
        else:
            result, reason = GameResult.ABORTED, EndReason.CHUDAN
        self._emit(GameEnded(result=result, reason=reason, detail=f"{self._reason_raw} {end.raw}".strip()))

    # ------------------------------------------------------------------ #
    # 送信
    # ------------------------------------------------------------------ #

    async def agree(self) -> None:
        await self._send("AGREE")

    def _check_can_commit(self, color: Color) -> None:
        if self._summary is None or not self._started:
            raise TransportError("対局が始まっていません")
        if self._ended:
            raise TransportError("対局は終了しています")
        if color is not self._summary.my_color:
            raise TransportError("自分の席ではありません")
        if self._turn() is not color:
            raise TransportError("自分の手番ではありません（送信しません）")
        if self._pending is not None:
            raise TransportError(f"直前に送った {self._pending} の確認待ちです（送信しません）")

    async def send_move(
        self, color: Color, usi: str, *, score: Score | None = None, pv: tuple[str, ...] = (),
        comment: str = "",
    ) -> None:
        self._check_can_commit(color)
        csa = csa_parse.usi_to_csa(self._board, usi)
        value = None
        pv_csa: tuple[str, ...] = ()
        comment = csa_parse.clean_comment(comment) if comment else ""
        if comment:
            self._pending = csa
            await self._send(csa_parse.build_move_line(csa, comment=comment))
            return
        if score is not None and self._settings.send_engine_comment:
            value = csa_parse.comment_score(score, color, self._settings.comment_score_pov)
            if value is not None and pv[:1] == (usi,):
                after = self._board.copy()
                after.push_usi(usi)
                pv_csa = csa_parse.pv_to_csa(after, tuple(pv[1:]))
        self._pending = csa
        await self._send(csa_parse.build_move_line(csa, score=value, pv_csa=pv_csa))

    async def send_resign(self, color: Color) -> None:
        self._check_can_commit(color)
        self._pending = "%TORYO"
        await self._send("%TORYO")

    async def send_declare(self, color: Color) -> None:
        """入玉宣言。条件成立は GameCore が確認済みであること。"""
        self._check_can_commit(color)
        self._pending = "%KACHI"
        await self._send("%KACHI")

    async def send_special(self, color: Color, text: str) -> None:
        """`%HIKIWAKE` など任意の `%X` を送る。サーバは指し手として扱うので、投了・宣言と同じ検査をする。"""
        line = csa_parse.clean_line(text)
        if csa_parse.classify_raw_line(line) not in ("special", "resign", "declare"):
            raise TransportError(f"特殊な指し手（%X）ではありません: {line!r}")
        self._check_can_commit(color)
        self._pending = line.split(",")[0]
        await self._send(line)

    async def send_raw(self, text: str) -> None:
        """対局の進行に関わらない行（拡張コマンドなど）をそのまま送る。

        指し手・`%X`・LOGIN / LOGOUT / AGREE / REJECT・コメント行は送らない（TransportError）。
        手番外に届いた指し手をサーバが保留して次の手番に適用してしまう事故を、ここでも防ぐ。
        """
        line = csa_parse.clean_line(text)
        kind = csa_parse.classify_raw_line(line)
        if kind not in ("extended", "other"):
            raise TransportError(f"この行は通信欄から直接は送れません（種類: {kind}）")
        if not self.connected:
            raise TransportError("接続していません")
        await self._send(line)
