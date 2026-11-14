"""USI の行の解析・生成（純関数。I/O なし）。

参照: SPEC §6.3、§6.4

呼び出し側（usi.py）が各行の `\\r` と改行を除去してから渡す前提。
未知のトークンや壊れた行で例外を出さず、None を返すか読める部分だけ返す
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


_INFO_INT_KEYS = {
    "depth": "depth",
    "seldepth": "seldepth",
    "nodes": "nodes",
    "nps": "nps",
    "hashfull": "hashfull",
    "time": "time_ms",
    "multipv": "multipv",
}
_INFO_SKIP_ONE = {"currmove", "currmovenumber", "cpuload", "tbhits", "refutation", "currline"}


def _to_int(token: str) -> int | None:
    try:
        return int(token)
    except ValueError:
        return None


def parse_bestmove(line: str) -> ParsedBestMove | None:
    """`bestmove X [ponder Y]` を解析する。bestmove 行でなければ None。"""
    tokens = line.split()
    if len(tokens) < 2 or tokens[0] != "bestmove":
        return None
    ponder = None
    if len(tokens) >= 4 and tokens[2] == "ponder":
        ponder = tokens[3]
    return ParsedBestMove(move=tokens[1], ponder=ponder)


def parse_info(line: str) -> ParsedInfo | None:
    """`info ...` を解析する。`score cp N [lowerbound|upperbound]` / `score mate [+-]N` を区別する。"""
    tokens = line.split()
    if not tokens or tokens[0] != "info":
        return None
    values: dict = {}
    i = 1
    n = len(tokens)
    while i < n:
        key = tokens[i]
        if key == "string":
            values["string"] = line.split("string", 1)[1].strip()
            break
        if key == "pv":
            values["pv"] = tuple(tokens[i + 1 :])
            break
        if key in _INFO_INT_KEYS and i + 1 < n:
            value = _to_int(tokens[i + 1])
            if value is not None:
                values[_INFO_INT_KEYS[key]] = value
            i += 2
            continue
        if key == "score" and i + 2 < n + 1:
            kind = tokens[i + 1] if i + 1 < n else ""
            raw = tokens[i + 2] if i + 2 < n else ""
            i += 3
            bound = None
            if i < n and tokens[i] in ("lowerbound", "upperbound"):
                bound = "lower" if tokens[i] == "lowerbound" else "upper"
                i += 1
            if kind == "cp":
                cp = _to_int(raw)
                if cp is not None:
                    values["score"] = Score(cp=cp, bound=bound)
            elif kind == "mate":
                # `mate +` / `mate -`（手数不明）は 0 手として符号だけ残せないので ±1 とみなす
                if raw in ("+", "-"):
                    mate = 1 if raw == "+" else -1
                else:
                    mate = _to_int(raw)
                if mate is not None:
                    values["score"] = Score(mate=mate, bound=bound)
            continue
        if key in _INFO_SKIP_ONE:
            i += 2
            continue
        i += 1
    return ParsedInfo(**values)


def parse_id(line: str) -> tuple[str, str] | None:
    """`id name ...` / `id author ...` → ("name", "...")。"""
    tokens = line.split(None, 2)
    if len(tokens) < 3 or tokens[0] != "id":
        return None
    return tokens[1], tokens[2].strip()


def build_position(start_sfen: str | None, moves: Sequence[str]) -> str:
    """`position startpos moves ...` または `position sfen <sfen> moves ...` を作る（I4: 常に全手順）。"""
    head = "position startpos" if start_sfen is None else f"position sfen {start_sfen}"
    if moves:
        return f"{head} moves {' '.join(moves)}"
    return head


def build_go(go_time: GoTime, *, ponder: bool = False) -> str:
    """`go [ponder] btime .. wtime .. (byoyomi .. | binc .. winc ..)` を作る。"""
    parts = ["go"]
    if ponder:
        parts.append("ponder")
    parts += ["btime", str(max(0, go_time.btime_ms)), "wtime", str(max(0, go_time.wtime_ms))]
    if go_time.binc_ms is not None or go_time.winc_ms is not None:
        parts += ["binc", str(max(0, go_time.binc_ms or 0)), "winc", str(max(0, go_time.winc_ms or 0))]
    else:
        parts += ["byoyomi", str(max(0, go_time.byoyomi_ms or 0))]
    return " ".join(parts)


def build_setoption(name: str, value: str | int | bool) -> str:
    """`setoption name <name> value <value>`。bool は true / false。"""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return f"setoption name {name} value {text}"


def build_setoptions(options: Mapping[str, str | int | bool]) -> list[str]:
    return [build_setoption(name, value) for name, value in options.items()]
