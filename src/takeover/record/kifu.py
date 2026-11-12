"""棋譜の書き出し（CSA。v1 で KIF）。

担当: M2（CSA。テスト: tests/test_kifu.py。§9 シナリオ 12 の手元棋譜側）、M7（KIF）
参照: SPEC §6.8

- 入力は MoveRecord の列と対局情報だけ（GameCore の内部に触らない）。文字列を作る部分は純関数。
- エンジンの手には評価値・読み筋をコメントで残す。
- mark_human_moves が True のときだけ、人間の手のコメントに印を付ける。
- 終局時のほか、切断・SIGINT・例外での終了時にも、そこまでの手順を保存する。
- ファイル名: <日時>_<GameID または local>_<先手名>_<後手名>.csa（使えない文字は _ に置換）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..core.types import EndReason, GameResult, MoveRecord, TimeControl


@dataclass(frozen=True, slots=True)
class GameInfo:
    game_id: str | None  # None は local
    black_name: str
    white_name: str
    started_at: str  # ISO 8601（壁時計）
    time_control: TimeControl
    start_sfen: str | None = None
    result: GameResult | None = None
    end_reason: EndReason | None = None


def kifu_filename(info: GameInfo, ext: str = "csa") -> str:
    raise NotImplementedError("M2")


def render_csa(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool) -> str:
    raise NotImplementedError("M2")


def render_kif(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool) -> str:
    raise NotImplementedError("M7")


def save(directory: Path, info: GameInfo, moves: Sequence[MoveRecord], *,
         formats: Sequence[str], mark_human_moves: bool) -> list[Path]:
    raise NotImplementedError("M2")
