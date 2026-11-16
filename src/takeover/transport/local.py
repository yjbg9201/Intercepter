"""LocalReferee: ローカル対局の進行役（SPEC §6.6）。transport.base.Transport の実装。

CSA サーバと同じ役割をプロセス内で果たす。GameCore から見ると CsaTransport と区別がない：
指し手を受け取り、`MoveConfirmed`（消費時間つき）を返し、終局なら `GameEnded` を返す。

- 自分の時計（core.clock.Clock）を持ち、消費時間と時間切れを判定する。時間切れは
  手番ごとに張るタイマー（call_later）で検出する。時刻は注入された now() を使う。
- 千日手は core.rules（4 回を自前で数える。cshogi の is_draw は使わない）。
- 入玉宣言は core.nyugyoku。不成立の宣言は宣言側の負け（GameCore が事前に止めるので通常は来ない）。
- 指し手の合法性は GameCore が確認済みだが、ここでも確認し、非合法なら指した側の負けにする。
- 盤面は進行役の内部状態として別に持つ（GameCore の盤面の複製ではなく、サーバ側の盤に当たる）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import cshogi

from ..core.clock import Clock, is_time_up
from ..core.events import GameEnded, GameOffered, GameStarted, MoveConfirmed
from ..core.nyugyoku import DeclarationResult, judge_declaration
from ..core.rules import RepetitionTracker, board_turn
from ..core.types import Color, EndReason, GameResult, NyugyokuRule, Score, TimeControl
from .base import TransportEmitFn


def _win(color: Color) -> GameResult:
    return GameResult.BLACK_WIN if color is Color.BLACK else GameResult.WHITE_WIN


class LocalReferee:
    def __init__(
        self,
        *,
        black_name: str,
        white_name: str,
        time_control: TimeControl,
        max_moves: int,
        nyugyoku_rule: NyugyokuRule,
        emit: TransportEmitFn,
        now: Callable[[], float] = time.monotonic,
        call_later: Callable | None = None,
        start_sfen: str | None = None,
        game_id: str = "local",
    ) -> None:
        self._names = {Color.BLACK: black_name, Color.WHITE: white_name}
        self._tc = time_control
        self._max_moves = max_moves
        self._rule = nyugyoku_rule
        self._emit = emit
        self._now = now
        self._call_later = call_later
        self._start_sfen = start_sfen
        self._game_id = game_id
        self._board = cshogi.Board(sfen=start_sfen) if start_sfen else cshogi.Board()
        self._clock = Clock(time_control, now=now)
        self._repetition = RepetitionTracker(self._board)
        self._moves = 0
        self._started = False
        self._ended = False
        self._timer = None

    @property
    def ended(self) -> bool:
        return self._ended

    async def start(self) -> None:
        self._emit(GameOffered(
            game_id=self._game_id,
            black_name=self._names[Color.BLACK],
            white_name=self._names[Color.WHITE],
            my_color=None,
            time_control=self._tc,
            start_sfen=self._start_sfen,
            max_moves=self._max_moves or None,
        ))

    async def agree(self) -> None:
        if self._started or self._ended:
            return
        self._started = True
        self._emit(GameStarted(game_id=self._game_id))
        self._start_turn()

    # ---- 時計 ----

    def _start_turn(self) -> None:
        turn = board_turn(self._board)
        self._clock.start_turn(turn)
        self._cancel_timer()
        after = self._clock.flag_after_ms(turn)
        if after is None:
            return
        call_later = self._call_later or asyncio.get_running_loop().call_later
        self._timer = call_later(after / 1000, self.check_flag)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def check_flag(self) -> bool:
        """手番側が時間切れなら終局させる。タイマーから呼ばれる（テストから直接呼んでもよい）。"""
        if self._ended or not self._started:
            return False
        turn = self._clock.running
        if turn is None:
            return False
        if is_time_up(self._clock.remaining_ms(turn), self._clock.consumed_now(), self._tc):
            self._end(_win(turn.opponent), EndReason.TIME_UP)
            return True
        return False

    def _end(self, result: GameResult, reason: EndReason, detail: str = "") -> None:
        if self._ended:
            return
        self._ended = True
        self._cancel_timer()
        self._clock.stop()
        self._emit(GameEnded(result=result, reason=reason, detail=detail))

    # ---- 指し手 ----

    def _playing(self, color: Color) -> bool:
        return self._started and not self._ended and board_turn(self._board) is color

    async def send_move(
        self, color: Color, usi: str, *, score: Score | None = None, pv: tuple[str, ...] = (),
        comment: str = "",
    ) -> None:
        if not self._playing(color):
            return  # 終局後・手番外の指し手は無視する（GameCore 側のレースで起こりうる）
        consumed = self._clock.consumed_now()
        if is_time_up(self._clock.remaining_ms(color), consumed, self._tc):
            self._end(_win(color.opponent), EndReason.TIME_UP)
            return
        try:
            move = self._board.move_from_usi(usi)
        except Exception:
            move = 0
        if not move or not self._board.is_legal(move):
            self._end(_win(color.opponent), EndReason.ILLEGAL_MOVE, detail=usi)
            return
        self._board.push(move)
        self._moves += 1
        self._clock.apply_move(color, consumed)
        self._emit(MoveConfirmed(color=color, usi=usi, elapsed_ms=consumed))

        if not any(True for _ in self._board.legal_moves):
            self._end(_win(color), EndReason.MATE)
            return
        repetition = self._repetition.push(self._board)
        if repetition is not None:
            if repetition.perpetual_checker is None:
                self._end(GameResult.DRAW, EndReason.SENNICHITE)
            else:
                self._end(_win(repetition.perpetual_checker.opponent), EndReason.OUTE_SENNICHITE)
            return
        if self._max_moves and self._moves >= self._max_moves:
            self._end(GameResult.DRAW, EndReason.MAX_MOVES)
            return
        self._start_turn()

    async def send_resign(self, color: Color) -> None:
        if self._playing(color):
            self._end(_win(color.opponent), EndReason.RESIGN)

    async def send_declare(self, color: Color) -> None:
        if not self._playing(color):
            return
        consumed = self._clock.consumed_now()
        has_time = not is_time_up(self._clock.remaining_ms(color), consumed, self._tc)
        result = judge_declaration(self._board, self._rule, has_time_left=has_time)
        if result is DeclarationResult.WIN:
            self._end(_win(color), EndReason.JISHOGI)
        elif result is DeclarationResult.DRAW:
            self._end(GameResult.DRAW, EndReason.JISHOGI)
        else:
            self._end(_win(color.opponent), EndReason.ILLEGAL_DECLARATION)

    async def close(self) -> None:
        self._cancel_timer()
