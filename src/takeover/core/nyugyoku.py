"""入玉宣言の判定（SPEC §6.6、§7.4）。

cshogi 1.1.1 の Board.nyugyoku_result(rule)（cshogi.LAW_27 / cshogi.LAW_24）の薄いラッパ。
cshogi は手番側について判定する。cshogi が見ない「持ち時間が残っていること」だけをここで足す。
"""

from __future__ import annotations

from enum import Enum

import cshogi

from .types import NyugyokuRule


class DeclarationResult(str, Enum):
    WIN = "win"
    DRAW = "draw"  # 24 点法のみ（24〜30 点）
    NONE = "none"  # 条件を満たさない（宣言すると負け）


def judge_declaration(board, rule: NyugyokuRule, *, has_time_left: bool = True) -> DeclarationResult:
    """board（cshogi.Board）の手番側が今宣言したときの結果。"""
    if not has_time_left:
        return DeclarationResult.NONE
    law = cshogi.LAW_24 if rule is NyugyokuRule.LAW_24 else cshogi.LAW_27
    result = board.nyugyoku_result(law)
    if result == cshogi.NYUGYOKU_WIN:
        return DeclarationResult.WIN
    if result == cshogi.NYUGYOKU_DRAW:
        return DeclarationResult.DRAW
    return DeclarationResult.NONE


def can_declare(board, rule: NyugyokuRule, *, has_time_left: bool = True) -> bool:
    """宣言ボタンを有効にしてよいか（WIN のときだけ True）。"""
    return judge_declaration(board, rule, has_time_left=has_time_left) is DeclarationResult.WIN
