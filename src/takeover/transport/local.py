"""LocalReferee: ローカル対局の進行役（SPEC §6.6）。transport.base.Transport の実装。

担当: M2（テスト: tests/test_local_referee.py。§9 シナリオ 20 ほか）

判定するもの：千日手（連続王手を含む）、入玉宣言、最大手数、詰み、時間切れの通知。
- 千日手は cshogi の is_draw() を使わない（2 回目で成立してしまう）。局面キーの出現回数を
  自分で数えて 4 回目で成立。連続王手は履歴から判定し、王手をかけ続けた側の負け。
- 入玉宣言は core.nyugyoku。不成立の宣言は宣言側の負け（GameCore が事前に止めるので通常は来ない）。
- 指し手の合法性は GameCore が確認済み。ここでは盤面を進めて終局条件を見るだけ。
- 時計は GameCore（core.clock）が持つ。時間切れは GameCore のタイマーが検出し、
  この進行役に終局を確定させる。
- 盤面を持つ必要があるが、GameCore の盤面とは別の複製（進行役の内部状態）として扱う（I1 に反しない）。
"""

from __future__ import annotations

from ..core.types import Color, NyugyokuRule, Score, TimeControl
from .base import TransportEmitFn


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
    ) -> None:
        self._emit = emit

    async def start(self) -> None:
        raise NotImplementedError("M2")

    async def agree(self) -> None:
        raise NotImplementedError("M2")

    async def send_move(
        self, color: Color, usi: str, *, score: Score | None = None, pv: tuple[str, ...] = ()
    ) -> None:
        raise NotImplementedError("M2")

    async def send_resign(self, color: Color) -> None:
        raise NotImplementedError("M2")

    async def send_declare(self, color: Color) -> None:
        raise NotImplementedError("M2")

    async def flag(self, color: Color) -> None:
        """color の時間切れを確定させる（GameCore のタイマーから呼ぶ。ローカル専用）。"""
        raise NotImplementedError("M2")

    async def close(self) -> None:
        raise NotImplementedError("M2")
