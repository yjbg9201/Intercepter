"""CSA プロトコルの行の解析・生成（純関数。I/O なし）。

担当: M0（テスト: tests/test_csa_parse.py）
参照: SPEC §6.5、§6.4（時間の単位）、§12（評価値の符号・mate の写像は未決）

指し手の CSA ⇔ USI 変換は盤面が必要なので、cshogi.Board を引数に取る。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.types import Color, EndReason, GameResult, Score, TimeControl


@dataclass(frozen=True, slots=True)
class GameSummary:
    game_id: str
    black_name: str
    white_name: str
    my_color: Color  # Your_Turn
    to_move: Color  # To_Move
    time_control: dict[Color, TimeControl]  # 先後で違う場合があるので色ごとに持つ
    start_sfen: str | None  # None は平手
    start_moves: tuple[str, ...]  # Position 内の途中手順（USI）
    max_moves: int | None
    declaration: str | None = None  # "Jishogi 1.1" など
    unknown: dict[str, str] = field(default_factory=dict)  # 未知のキー（ログ用）


@dataclass(frozen=True, slots=True)
class ServerMove:
    color: Color
    csa: str  # "+7776FU" / "%TORYO" / "%KACHI"
    elapsed_units: int | None  # T の値（Time_Unit 単位）
    comment: str | None = None  # 通常は届かない（SPEC §6.5）


def build_login(name: str, password: str, *, x1: bool = False) -> str:
    """`LOGIN <name> <password>[ x1]`。"""
    raise NotImplementedError("M0")


def build_floodgate_password(game: str, trip: str) -> str:
    """`<ゲーム名>,<識別文字列>`。"""
    raise NotImplementedError("M0")


def mask_login(line: str) -> str:
    """ログ用に LOGIN 行のパスワード部分を伏せる（I10）。LOGIN 行以外はそのまま返す。"""
    raise NotImplementedError("M0")


def parse_game_summary(lines: list[str]) -> GameSummary:
    """BEGIN Game_Summary 〜 END Game_Summary の行を解析する。"""
    raise NotImplementedError("M0")


def parse_server_move(line: str) -> ServerMove | None:
    """`+7776FU,T12` / `%TORYO,T3` を解析する。指し手行でなければ None。"""
    raise NotImplementedError("M0")


def parse_end_line(line: str) -> EndReason | GameResult | str | None:
    """`#RESIGN` などの理由行は EndReason、`#WIN` / `#LOSE` / `#DRAW` / `#CENSORED` は

    結果を表す文字列（"win" / "lose" / "draw" / "censored"。色への変換は呼び出し側）で返す。
    `#` で始まらない行は None。
    """
    raise NotImplementedError("M0")


def usi_to_csa(board, usi: str) -> str:
    """USI の指し手を、board（指す前の局面）での CSA 表記 `+7776FU` にする。"""
    raise NotImplementedError("M0")


def csa_to_usi(board, csa: str) -> str:
    raise NotImplementedError("M0")


def comment_score(score: Score, mover: Color, pov: str) -> int:
    """評価値コメントに書く整数。pov は "mover"（指した側視点）/ "black"（先手視点）。

    mate の写像の具体値は SPEC §12 の未決事項。符号の規約はこの関数だけに閉じ込める。
    """
    raise NotImplementedError("M3")


def build_move_line(csa: str, *, score: int | None = None, pv_csa: tuple[str, ...] = ()) -> str:
    """送信する指し手行。score があれば `,'* <評価値> <読み筋>` を付ける。

    人間の手では score / pv を渡さない（コメントなしの行になる）。
    """
    raise NotImplementedError("M0")
