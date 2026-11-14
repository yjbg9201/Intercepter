"""対局規則のうち cshogi にそのまま任せられないもの（SPEC §6.6）。

千日手は cshogi の Board.is_draw() を使わない（同一局面の 2 回目で成立してしまう）。
局面キー（zobrist_hash。盤・持ち駒・手番を含む）の出現回数を数え、4 回目で成立とする。
"""

from __future__ import annotations

from dataclasses import dataclass

import cshogi

from .types import Color


def board_turn(board) -> Color:
    return Color.BLACK if board.turn == cshogi.BLACK else Color.WHITE


@dataclass(frozen=True, slots=True)
class Repetition:
    """千日手の成立。perpetual_checker が入っていれば連続王手の千日手で、その側の負け。"""

    perpetual_checker: Color | None = None


class RepetitionTracker:
    """局面の履歴を持ち、着手のたびに千日手の成立を判定する。"""

    def __init__(self, board) -> None:
        # 各要素: (局面キー, その局面で手番側に王手がかかっているか, 手番)
        self._history: list[tuple[int, bool, Color]] = []
        self._record(board)

    def _record(self, board) -> None:
        self._history.append((board.zobrist_hash(), bool(board.is_check()), board_turn(board)))

    def push(self, board) -> Repetition | None:
        """着手後の board を記録する。同一局面が 4 回目なら Repetition を返す。"""
        self._record(board)
        key = self._history[-1][0]
        indices = [i for i, (k, _, _) in enumerate(self._history) if k == key]
        if len(indices) < 4:
            return None
        first = indices[0]
        # first より後の各局面は「直前に指した側」が作った。王手がかかっていれば、その手は王手。
        for checker in Color:
            flags = [in_check for _, in_check, turn in self._history[first + 1 :] if turn is checker.opponent]
            if flags and all(flags):
                return Repetition(perpetual_checker=checker)
        return Repetition()
