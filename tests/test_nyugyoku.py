"""27 点法の宣言判定（SPEC §6.6、§9 シナリオ 14 の判定部分）。"""

import cshogi
import pytest

from takeover.core.nyugyoku import DeclarationResult, can_declare, judge_declaration
from takeover.core.types import NyugyokuRule

R27 = NyugyokuRule.LAW_27

# 先手玉 5a。敵陣に飛飛角角（20 点）＋金4銀2（6 点）＝ 10 枚 26 点、持ち駒の歩で調整する。
WIN_28 = "RRBBK4/GGGGSS3/9/9/9/9/9/9/4k4 b 2P 1"
POINTS_27 = "RRBBK4/GGGGSS3/9/9/9/9/9/9/4k4 b P 1"
NINE_PIECES = "RRBBK4/GGGGS4/9/9/9/9/9/9/4k4 b 3P 1"
IN_CHECK = "RRBBK4/GGGG1SS2/9/9/4r4/9/9/9/4k4 b 2P 1"
KING_OUTSIDE = "RRBB5/GGGGSS3/9/4K4/9/9/9/9/4k4 b 2P 1"
# 後手は 27 点でよい（後手玉 5i、後手の駒が 7〜9 段目）
WHITE_27 = "4K4/9/9/9/9/9/9/ggggss3/rrbbk4 w p 1"
WHITE_26 = "4K4/9/9/9/9/9/9/ggggss3/rrbbk4 w - 1"


@pytest.mark.parametrize(
    "sfen, expected",
    [
        (WIN_28, DeclarationResult.WIN),
        (POINTS_27, DeclarationResult.NONE),
        (NINE_PIECES, DeclarationResult.NONE),
        (IN_CHECK, DeclarationResult.NONE),
        (KING_OUTSIDE, DeclarationResult.NONE),
        (WHITE_27, DeclarationResult.WIN),
        (WHITE_26, DeclarationResult.NONE),
    ],
)
def test_27_point_rule(sfen, expected):
    board = cshogi.Board(sfen=sfen)
    assert judge_declaration(board, R27) is expected
    assert can_declare(board, R27) is (expected is DeclarationResult.WIN)


def test_no_time_left_means_no_declaration():
    assert judge_declaration(cshogi.Board(sfen=WIN_28), R27, has_time_left=False) is DeclarationResult.NONE


def test_startpos_cannot_declare():
    assert not can_declare(cshogi.Board(), R27)
