"""棋譜の書き出し（CSA。v1 で KIF）。

参照: SPEC §6.8

- 入力は MoveRecord の列と対局情報だけ（GameCore の内部に触らない）。文字列を作る部分は純関数。
- エンジンの手には評価値・読み筋をコメントで残す（Floodgate の棋譜と同じ `'** 評価値 読み筋` 形式。
  評価値は指した側視点）。
- mark_human_moves が True のときだけ、人間の手のコメントに印（`'human`）を付ける。
- ファイル名: <日時>_<GameID または local>_<先手名>_<後手名>.csa（使えない文字は _ に置換）
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cshogi

from ..core.types import Actor, Color, EndReason, GameResult, MoveRecord, TimeControl
from ..transport.csa_parse import pv_to_csa, score_to_int

HUMAN_MARK = "'human"

_END_MARKS = {
    EndReason.RESIGN: "%TORYO",
    EndReason.TIME_UP: "%TIME_UP",
    EndReason.ILLEGAL_MOVE: "%ILLEGAL_MOVE",
    EndReason.SENNICHITE: "%SENNICHITE",
    EndReason.OUTE_SENNICHITE: "%OUTE_SENNICHITE",
    EndReason.JISHOGI: "%KACHI",
    EndReason.ILLEGAL_DECLARATION: "%ILLEGAL_MOVE",
    EndReason.MAX_MOVES: "%MAX_MOVES",
    EndReason.MATE: "%TSUMI",
    EndReason.CHUDAN: "%CHUDAN",
    EndReason.DISCONNECTED: "%CHUDAN",
}


@dataclass(frozen=True, slots=True)
class GameInfo:
    game_id: str | None  # None は local
    black_name: str
    white_name: str
    started_at: str  # ISO 8601（壁時計）
    time_control: TimeControl | None = None
    start_sfen: str | None = None
    start_moves: tuple[str, ...] = ()
    result: GameResult | None = None
    end_reason: EndReason | None = None


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", text) or "_"


def kifu_filename(info: GameInfo, ext: str = "csa") -> str:
    stamp = re.sub(r"[^0-9]", "", info.started_at)[:14] or "00000000000000"
    stamp = f"{stamp[:8]}_{stamp[8:]}" if len(stamp) > 8 else stamp
    game = _safe(info.game_id) if info.game_id else "local"
    return f"{stamp}_{game}_{_safe(info.black_name)}_{_safe(info.white_name)}.{ext}"


def render_csa(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool) -> str:
    board = cshogi.Board(sfen=info.start_sfen) if info.start_sfen else cshogi.Board()
    lines = ["V2.2", f"N+{info.black_name}", f"N-{info.white_name}"]
    if info.game_id:
        lines.append(f"$EVENT:{info.game_id}")
    lines.append(f"$START_TIME:{info.started_at}")
    if info.time_control is not None:
        tc = info.time_control
        lines.append(f"'Total_Time:{tc.total_ms // 1000} Byoyomi:{tc.byoyomi_ms // 1000} "
                     f"Increment:{tc.increment_ms // 1000}")
    lines += [l for l in board.csa_pos().split("\n") if l]  # 盤面・持ち駒・手番

    def push(usi: str) -> None:
        sign = "+" if board.turn == cshogi.BLACK else "-"
        move = board.move_from_usi(usi)
        lines.append(sign + cshogi.move_to_csa(move))
        board.push(move)

    for usi in info.start_moves:
        push(usi)
    for record in moves:
        push(record.usi)
        if record.elapsed_ms is not None:
            lines.append(f"T{record.elapsed_ms // 1000}")
        if record.actor is Actor.ENGINE and record.score is not None:
            value = score_to_int(record.score)
            if value is not None:
                # pv の先頭は指した手自身なので、その次（相手の応手）から書く
                board.pop()
                pv = pv_to_csa(board, record.pv)[1:] if record.pv[:1] == (record.usi,) else ()
                board.push_usi(record.usi)
                lines.append(" ".join(["'**", str(value), *pv]))
        if record.actor is Actor.HUMAN and mark_human_moves:
            lines.append(HUMAN_MARK)
    if info.end_reason in _END_MARKS:
        lines.append(_END_MARKS[info.end_reason])
    if info.result is not None:
        lines.append(f"'summary:{info.end_reason.value if info.end_reason else ''}:{info.result.value}")
    return "\n".join(lines) + "\n"


def render_kif(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool) -> str:
    raise NotImplementedError("M7")


def save(directory: Path, info: GameInfo, moves: Sequence[MoveRecord], *,
         formats: Sequence[str], mark_human_moves: bool) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for format_ in formats:
        if format_ == "csa":
            text = render_csa(info, moves, mark_human_moves=mark_human_moves)
        elif format_ == "kif":
            try:
                text = render_kif(info, moves, mark_human_moves=mark_human_moves)
            except NotImplementedError:
                continue
        else:
            continue
        path = directory / kifu_filename(info, format_)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
