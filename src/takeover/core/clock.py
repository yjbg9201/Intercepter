"""時計と、USI に渡す時間引数の計算（SPEC §6.4）。

参照: SPEC §6.4、§7.4（TimeGuard）、§12 の Q5

規則（shogi-server の time_clock.rb に合わせる）：
- 消費時間 t = max(経過を time_unit に切り捨て, least_time_per_move)
- 着手ごとに「残り += increment」してから「残り -= t」（下限 0）
- 時間切れ: 残り - t + byoyomi + increment <= 0
- CSA モードではサーバの T が正。apply_move() に T を渡して確定する。
  ローカルモードでは自前で計測した経過から同じ規則で t を作る。

時刻は注入された now() だけを使う。計算は純関数に寄せ、Clock は残り時間と手番開始時刻を持つだけ。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from ..engine.base import GoTime
from .types import Color, TimeControl


def consumed_ms(elapsed_ms: int, tc: TimeControl) -> int:
    """経過時間から、時計から引く消費時間を求める（純関数）。"""
    unit = max(1, tc.time_unit_ms)
    return max((max(0, elapsed_ms) // unit) * unit, tc.least_time_per_move_ms)


def has_time_limit(tc: TimeControl) -> bool:
    return tc.total_ms > 0 or tc.byoyomi_ms > 0 or tc.increment_ms > 0


def is_time_up(remaining_ms: int, consumed: int, tc: TimeControl) -> bool:
    """その消費時間で時間切れになるか（純関数）。"""
    if not has_time_limit(tc):
        return False
    return remaining_ms - consumed + tc.byoyomi_ms + tc.increment_ms <= 0


def flag_after_ms(remaining_ms: int, tc: TimeControl) -> int | None:
    """手番開始から何 ms 経つと時間切れになるか（純関数）。制限なしなら None。"""
    if not has_time_limit(tc):
        return None
    unit = max(1, tc.time_unit_ms)
    budget = max(0, remaining_ms + tc.byoyomi_ms + tc.increment_ms)
    limit = -(-budget // unit) * unit  # 単位に切り上げ
    return max(limit, unit)


def go_time_for(
    *,
    turn: Color,
    remaining_ms: Mapping[Color, int],
    turn_elapsed_ms: int,
    tcs: Mapping[Color, TimeControl],
    margin_ms: int,
) -> GoTime:
    """`go` に渡す時間（純関数）。送る直前に毎回計算する。

    手番側だけ既経過と margin を引く。残りを超えた既経過は byoyomi から引く。すべて下限 0。
    ponder 用は turn_elapsed_ms=0 で呼ぶ。フィッシャーの btime には、その手の加算分を含めない
    （SPEC §12 の未決事項。安全側）。
    """
    elapsed = max(0, turn_elapsed_ms)
    times = {}
    for color in Color:
        value = remaining_ms[color]
        if color is turn:
            value = value - elapsed - margin_ms
        times[color] = max(0, value)
    overflow = max(0, elapsed - remaining_ms[turn])
    tc = tcs[turn]
    if tcs[Color.BLACK].increment_ms or tcs[Color.WHITE].increment_ms:
        return GoTime(
            btime_ms=times[Color.BLACK],
            wtime_ms=times[Color.WHITE],
            binc_ms=tcs[Color.BLACK].increment_ms,
            winc_ms=tcs[Color.WHITE].increment_ms,
        )
    byoyomi = max(0, tc.byoyomi_ms - overflow - margin_ms) if tc.byoyomi_ms else 0
    return GoTime(btime_ms=times[Color.BLACK], wtime_ms=times[Color.WHITE], byoyomi_ms=byoyomi)


def manual_budget_ms(remaining_ms: int, turn_elapsed_ms: int, tc: TimeControl) -> int:
    """TimeGuard の判定に使う「この手番であと使える時間」（純関数。SPEC §12 Q5）。"""
    return remaining_ms - max(0, turn_elapsed_ms) + tc.byoyomi_ms + tc.increment_ms


class Clock:
    def __init__(
        self,
        tcs: Mapping[Color, TimeControl] | TimeControl,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(tcs, TimeControl):
            tcs = {Color.BLACK: tcs, Color.WHITE: tcs}
        self.tcs: dict[Color, TimeControl] = dict(tcs)
        self._now = now
        self._remaining = {c: self.tcs[c].total_ms for c in Color}
        self._running: Color | None = None
        self._started = 0.0

    @property
    def running(self) -> Color | None:
        return self._running

    def set_remaining(self, color: Color, remaining_ms: int) -> None:
        self._remaining[color] = max(0, remaining_ms)

    def start_turn(self, color: Color) -> None:
        """color の時計を動かし始める。"""
        self._running = color
        self._started = self._now()

    def turn_elapsed_ms(self) -> int:
        """動いている側の、本手番の既経過。"""
        if self._running is None:
            return 0
        return max(0, round((self._now() - self._started) * 1000))

    def consumed_now(self) -> int:
        """今この瞬間に着手した場合の消費時間（ローカルモード用）。"""
        if self._running is None:
            return 0
        return consumed_ms(self.turn_elapsed_ms(), self.tcs[self._running])

    def apply_move(self, color: Color, consumed: int) -> None:
        """着手を時計に反映する（加算 → 減算）。CSA モードは T、ローカルは consumed_now() の値。"""
        tc = self.tcs[color]
        self._remaining[color] = max(0, self._remaining[color] + tc.increment_ms - max(0, consumed))
        if self._running is color:
            self._running = None

    def remaining_ms(self, color: Color) -> int:
        """手番開始時点の残り（本手番の既経過は引いていない値）。"""
        return self._remaining[color]

    def remaining_map(self) -> dict[Color, int]:
        return dict(self._remaining)

    def flag_after_ms(self, color: Color) -> int | None:
        return flag_after_ms(self._remaining[color], self.tcs[color])

    def go_time(self, turn: Color, *, margin_ms: int, pondering: bool = False) -> GoTime:
        elapsed = 0 if pondering or self._running is not turn else self.turn_elapsed_ms()
        return go_time_for(turn=turn, remaining_ms=self._remaining, turn_elapsed_ms=elapsed,
                           tcs=self.tcs, margin_ms=margin_ms)

    def manual_budget_ms(self, color: Color) -> int:
        elapsed = self.turn_elapsed_ms() if self._running is color else 0
        return manual_budget_ms(self._remaining[color], elapsed, self.tcs[color])

    def stop(self) -> None:
        """終局。時計を止める。"""
        self._running = None
