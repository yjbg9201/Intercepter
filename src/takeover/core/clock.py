"""時計と、USI に渡す時間引数の計算（SPEC §6.4）。

担当: M2（テスト: tests/test_clock.py。§9 シナリオ 4 と shogi-server 規則の検算）
参照: SPEC §6.4、§7.4（TimeGuard）、§12 の Q5

規則（shogi-server の time_clock.rb に合わせる）：
- 消費時間 t = max(経過を time_unit に切り捨て, least_time_per_move)
- 着手ごとに「残り += increment」してから「残り -= t」（下限 0）
- 時間切れ: 残り - t + byoyomi + increment <= 0
- CSA モードではサーバの T が正。apply_move() に T を渡して確定する。
  ローカルモードでは自前で計測した経過から同じ規則で t を作る。

時刻は注入された now() だけを使う。計算は純関数に寄せ、Clock は残り時間と手番開始時刻を持つだけにする。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..engine.base import GoTime
from .types import Color, TimeControl


def consumed_ms(elapsed_ms: int, tc: TimeControl) -> int:
    """経過時間から、時計から引く消費時間を求める（純関数）。"""
    raise NotImplementedError("M2")


def is_time_up(remaining_ms: int, consumed: int, tc: TimeControl) -> bool:
    """その消費時間で時間切れになるか（純関数）。"""
    raise NotImplementedError("M2")


def go_time_for(
    *,
    turn: Color,
    remaining_ms: dict[Color, int],
    turn_elapsed_ms: int,
    tc: TimeControl,
    margin_ms: int,
) -> GoTime:
    """`go` に渡す時間（純関数）。送る直前に毎回計算する。

    手番側だけ既経過と margin を引く。残りを超えた既経過は byoyomi から引く。すべて下限 0。
    ponder 用は turn_elapsed_ms=0 で呼ぶ。
    """
    raise NotImplementedError("M2")


def manual_budget_ms(remaining_ms: int, turn_elapsed_ms: int, tc: TimeControl) -> int:
    """TimeGuard の判定に使う「この手番であと使える時間」（純関数。SPEC §12 Q5 の仮決め）。"""
    raise NotImplementedError("M5")


class Clock:
    def __init__(self, tc: TimeControl, *, now: Callable[[], float] = time.monotonic) -> None:
        self._tc = tc
        self._now = now

    def start_turn(self, color: Color) -> None:
        """color の時計を動かし始める。"""
        raise NotImplementedError("M2")

    def turn_elapsed_ms(self) -> int:
        """動いている側の、本手番の既経過。"""
        raise NotImplementedError("M2")

    def apply_move(self, color: Color, consumed: int) -> None:
        """着手を時計に反映する（加算 → 減算）。CSA モードは T、ローカルは consumed_ms() の値。"""
        raise NotImplementedError("M2")

    def remaining_ms(self, color: Color) -> int:
        """手番開始時点の残り（本手番の既経過は引いていない値）。"""
        raise NotImplementedError("M2")

    def stop(self) -> None:
        """終局。時計を止める。"""
        raise NotImplementedError("M2")
