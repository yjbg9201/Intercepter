"""USI の行の解析・生成（純関数。I/O なし）。

担当: M0（テスト: tests/test_usi_parse.py）
参照: SPEC §6.3、§6.4

呼び出し側（usi.py）が各行の `\\r` と改行を除去してから渡す前提。
未知のトークンや壊れた行で例外を出さず、None を返すか読める部分だけ返すこと
（エンジンの出力で対局を止めない）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..core.types import Score
from .base import GoTime


@dataclass(frozen=True, slots=True)
class ParsedBestMove:
    move: str  # USI の指し手、"resign"、"win"
    ponder: str | None


@dataclass(frozen=True, slots=True)
class ParsedInfo:
    depth: int | None = None
    seldepth: int | None = None
    score: Score | None = None
    nodes: int | None = None
    nps: int | None = None
    hashfull: int | None = None
    time_ms: int | None = None
    multipv: int = 1
    pv: tuple[str, ...] = ()
    string: str | None = None  # `info string ...`


def parse_bestmove(line: str) -> ParsedBestMove | None:
    """`bestmove X [ponder Y]` を解析する。bestmove 行でなければ None。"""
    raise NotImplementedError("M0")


def parse_info(line: str) -> ParsedInfo | None:
    """`info ...` を解析する。`score cp N [lowerbound|upperbound]` / `score mate [+-]N` を区別する。"""
    raise NotImplementedError("M0")


def parse_id(line: str) -> tuple[str, str] | None:
    """`id name ...` / `id author ...` → ("name", "...")。"""
    raise NotImplementedError("M0")


def build_position(start_sfen: str | None, moves: Sequence[str]) -> str:
    """`position startpos moves ...` または `position sfen <sfen> moves ...` を作る（I4: 常に全手順）。"""
    raise NotImplementedError("M0")


def build_go(go_time: GoTime, *, ponder: bool = False) -> str:
    """`go [ponder] btime .. wtime .. (byoyomi .. | binc .. winc ..)` を作る。"""
    raise NotImplementedError("M0")


def build_setoption(name: str, value: str | int | bool) -> str:
    """`setoption name <name> value <value>`。bool は true / false。"""
    raise NotImplementedError("M0")


def build_setoptions(options: Mapping[str, str | int | bool]) -> list[str]:
    raise NotImplementedError("M0")
