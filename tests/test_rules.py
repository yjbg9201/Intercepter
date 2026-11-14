"""千日手（SPEC §6.6、§9 シナリオ 20）。"""

import cshogi

from takeover.core.rules import RepetitionTracker
from takeover.core.types import Color


def play(board, tracker, moves):
    results = []
    for usi in moves:
        assert board.push_usi(usi), usi
        results.append(tracker.push(board))
    return results


def test_fourfold_not_twofold():
    board = cshogi.Board()
    tracker = RepetitionTracker(board)
    cycle = ["5i5h", "5a5b", "5h5i", "5b5a"]
    results = play(board, tracker, cycle * 3)
    # 初期局面は 1 回目。cycle 1 周ごとに 2, 3, 4 回目。
    assert results[:11] == [None] * 11
    assert results[11] is not None and results[11].perpetual_checker is None


def test_perpetual_check_loses():
    # 先手の飛車が 5 筋と 4 筋を往復して王手を続け、後手玉が同じ筋を逃げ続ける
    board = cshogi.Board(sfen="5k3/9/9/9/9/9/9/9/K3R4 b - 1")
    tracker = RepetitionTracker(board)
    cycle = ["5i4i", "4a5a", "4i5i", "5a4a"]
    results = play(board, tracker, cycle * 3)
    final = [r for r in results if r is not None]
    assert final and final[0].perpetual_checker is Color.BLACK
