"""入玉宣言の判定（SPEC §6.6、§7.4）。

担当: M2（27 点法。テスト: tests/test_nyugyoku.py。§9 シナリオ 14）、M9（24 点法）

cshogi 1.1.1 の Board.is_nyugyoku(rule) / Board.nyugyoku_result(rule)
（cshogi.LAW_27 / cshogi.LAW_24）の薄いラッパにする。cshogi は手番側について判定する。
cshogi が見ない「持ち時間が残っていること」だけをここで足す。

テストでは SPEC §6.6 に書いた条件（成立／点数不足／駒数不足／王手中／玉が敵陣外）の
各局面で cshogi の結果が期待どおりかを確かめる（ラッパでも条件表は自分たちで検証する）。
"""

from __future__ import annotations

from enum import Enum

from .types import NyugyokuRule


class DeclarationResult(str, Enum):
    WIN = "win"
    DRAW = "draw"  # 24 点法のみ（24〜30 点）
    NONE = "none"  # 条件を満たさない（宣言すると負け）


def judge_declaration(board, rule: NyugyokuRule, *, has_time_left: bool) -> DeclarationResult:
    """board（cshogi.Board）の手番側が今宣言したときの結果。"""
    raise NotImplementedError("M2")


def can_declare(board, rule: NyugyokuRule, *, has_time_left: bool) -> bool:
    """宣言ボタンを有効にしてよいか（WIN のときだけ True）。"""
    return judge_declaration(board, rule, has_time_left=has_time_left) is DeclarationResult.WIN
