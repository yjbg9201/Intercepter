"""棋譜の書き出し（CSA / KIF）。

参照: SPEC §6.8

- 入力は MoveRecord の列と対局情報だけ（GameCore の内部に触らない）。文字列を作る部分は純関数。
- エンジンの手には評価値・読み筋をコメントで残す（CSA は Floodgate の棋譜と同じ `'** 評価値 読み筋` 形式）。
  評価値の視点は score_pov（既定 "black"＝先手視点。サーバへ送るコメントと同じ設定 csa.comment_score_pov）。
- mark_human_moves が True のときだけ、人間の手のコメントに印（CSA は `'human`、KIF は `*人間の手`）を付ける。
- 形式（record.formats）: "csa"（.csa）/ "kif"（.kif、Shift_JIS。多くの棋譜ソフトの既定）/ "kifu"（.kifu、UTF-8）。
- ファイル名: <日時>_<GameID または local>_<先手名>_<後手名>.<拡張子>（使えない文字は _ に置換）。
  同じ名前のファイルがあれば上書きせず、末尾に _2, _3 … を付ける。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cshogi

from ..core.types import Actor, Color, EndReason, GameResult, MoveRecord, TimeControl
from ..transport.csa_parse import comment_score, pv_to_csa

HUMAN_MARK = "'human"
KIF_HUMAN_MARK = "*人間の手"

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


def render_csa(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool,
               score_pov: str = "black") -> str:
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
        if record.score is not None:  # エンジンの手。人間の手は hint.send_as_comment のときだけ付いている
            value = comment_score(record.score, record.color, score_pov)
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


# ---- KIF ------------------------------------------------------------------ #

_ZEN = "０１２３４５６７８９"
_KAN = "〇一二三四五六七八九"
# cshogi の駒種（1 歩 … 8 玉、9 と … 14 龍）→ KIF の駒名
_KIF_PIECES = ["", "歩", "香", "桂", "銀", "角", "飛", "金", "玉", "と", "成香", "成桂", "成銀", "馬", "龍"]
_KIF_DROPS = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛"}
_KIF_BOARD = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛", "K": "玉",
              "+P": "と", "+L": "杏", "+N": "圭", "+S": "全", "+B": "馬", "+R": "龍"}
_HAND_ORDER = "RBGSNLP"


def _kan_number(n: int) -> str:
    """1〜99 の漢数字（持ち駒の枚数用）。"""
    tens, ones = divmod(n, 10)
    return ("" if tens == 0 else ("十" if tens == 1 else _KAN[tens] + "十")) + (_KAN[ones] if ones else "")


def kif_move(board, usi: str, previous_to: str | None = None) -> str:
    """`７六歩(77)` / `同　銀(31)` / `５五角打` / `２二角成(88)`。board は指す前の局面（変更しない）。"""
    dest = usi[2:4]
    file_, rank = int(dest[0]), ord(dest[1]) - ord("a") + 1
    where = "同　" if previous_to == dest else _ZEN[file_] + _KAN[rank]
    if usi[1] == "*":
        return f"{where}{_KIF_DROPS[usi[0]]}打"
    src_file, src_rank = int(usi[0]), ord(usi[1]) - ord("a") + 1
    piece = _KIF_PIECES[board.piece_type((src_file - 1) * 9 + (src_rank - 1))]
    return f"{where}{piece}{'成' if usi.endswith('+') else ''}({src_file}{src_rank})"


def _kif_position(sfen: str) -> list[str]:
    """平手以外の開始局面を、KIF の局面図（BOD 形式）にする。"""
    board_part, turn, hand_part = (sfen.split() + ["b", "-"])[:3]
    hands: dict[str, dict[str, int]] = {"b": {}, "w": {}}
    count = ""
    for ch in "" if hand_part == "-" else hand_part:
        if ch.isdigit():
            count += ch
            continue
        hands["b" if ch.isupper() else "w"][ch.upper()] = int(count) if count else 1
        count = ""

    def hand_text(side: str) -> str:
        parts = [_KIF_DROPS[p] + (_kan_number(hands[side][p]) if hands[side][p] > 1 else "")
                 for p in _HAND_ORDER if hands[side].get(p)]
        return "　".join(parts) if parts else "なし"

    lines = [f"後手の持駒：{hand_text('w')}", "  ９ ８ ７ ６ ５ ４ ３ ２ １", "+---------------------------+"]
    for index, row in enumerate(board_part.split("/")):
        cells: list[str] = []
        promoted = False
        for ch in row:
            if ch == "+":
                promoted = True
            elif ch.isdigit():
                cells += [" ・"] * int(ch)
            else:
                name = _KIF_BOARD[("+" if promoted else "") + ch.upper()]
                cells.append(("v" if ch.islower() else " ") + name)
                promoted = False
        lines.append("|" + "".join(cells) + "|" + _KAN[index + 1])
    lines += ["+---------------------------+", f"先手の持駒：{hand_text('b')}"]
    if turn == "w":
        lines.append("後手番")
    return lines


def _kif_time_control(tc: TimeControl) -> str:
    def span(ms: int) -> str:
        sec = ms // 1000
        return f"{sec // 60}分" + (f"{sec % 60}秒" if sec % 60 else "") if sec >= 60 else f"{sec}秒"

    text = span(tc.total_ms)
    if tc.byoyomi_ms:
        text += f"+秒読み{span(tc.byoyomi_ms)}"
    if tc.increment_ms:
        text += f"+1手{span(tc.increment_ms)}加算"
    return text


def _kif_end(info: GameInfo, side_to_move: Color) -> tuple[str | None, str]:
    """(終局の指し手欄の語, 「まで…」に続ける結果の文)。"""
    result, reason = info.result, info.end_reason
    mover_wins = (result is GameResult.BLACK_WIN) == (side_to_move is Color.BLACK)
    if result is GameResult.BLACK_WIN:
        outcome = "先手の勝ち"
    elif result is GameResult.WHITE_WIN:
        outcome = "後手の勝ち"
    elif reason is EndReason.SENNICHITE:
        outcome = "千日手"
    elif reason in (EndReason.JISHOGI, EndReason.MAX_MOVES):
        outcome = "持将棋"
    else:
        outcome = "中断"
    decided = result in (GameResult.BLACK_WIN, GameResult.WHITE_WIN)
    word: str | None
    if reason is EndReason.RESIGN:
        word = "投了"
    elif reason is EndReason.TIME_UP:
        word = "切れ負け"
    elif reason is EndReason.MATE:
        word = "詰み"
    elif reason is EndReason.SENNICHITE:
        word = "千日手"
    elif reason is EndReason.JISHOGI:
        word = "入玉勝ち" if decided else "持将棋"
    elif reason is EndReason.MAX_MOVES:
        word = "持将棋"
    elif reason in (EndReason.ILLEGAL_MOVE, EndReason.OUTE_SENNICHITE, EndReason.ILLEGAL_DECLARATION) and decided:
        word = "反則勝ち" if mover_wins else "反則負け"
    elif reason is None and result is None:
        word = None
    else:
        word = "中断"
    return word, outcome


def render_kif(info: GameInfo, moves: Sequence[MoveRecord], *, mark_human_moves: bool,
               score_pov: str = "black", utf8_header: bool = False) -> str:
    """KIF（柿木形式）。utf8_header は .kifu（UTF-8）用の 1 行目を付ける。"""
    board = cshogi.Board(sfen=info.start_sfen) if info.start_sfen else cshogi.Board()
    lines: list[str] = ["#KIF version=2.0 encoding=UTF-8"] if utf8_header else []
    started = re.sub(r"[T]", " ", info.started_at[:19]).replace("-", "/")
    lines.append(f"開始日時：{started}")
    if info.game_id:
        lines.append(f"棋戦：{info.game_id}")
    if info.time_control is not None:
        lines.append(f"持ち時間：{_kif_time_control(info.time_control)}")
    if info.start_sfen:
        lines += _kif_position(info.start_sfen)
    else:
        lines.append("手合割：平手")
    lines += [f"先手：{info.black_name}", f"後手：{info.white_name}", "手数----指手---------消費時間--"]

    totals = {Color.BLACK: 0, Color.WHITE: 0}
    number = 0
    previous: str | None = None

    def add_move(usi: str, elapsed_ms: int | None) -> Color:
        nonlocal number, previous
        number += 1
        color = Color.BLACK if board.turn == cshogi.BLACK else Color.WHITE
        text = kif_move(board, usi, previous)
        previous = usi[2:4]
        board.push_usi(usi)
        sec = (elapsed_ms or 0) // 1000
        totals[color] += sec
        total = totals[color]
        lines.append(f"{number:4d} {text:<12s} ({sec // 60:2d}:{sec % 60:02d}/"
                     f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d})")
        return color

    for usi in info.start_moves:
        add_move(usi, None)
    for record in moves:
        before = board.copy()
        add_move(record.usi, record.elapsed_ms)
        if record.score is not None:
            value = comment_score(record.score, record.color, score_pov)
            if value is not None:
                pv = record.pv[1:] if record.pv[:1] == (record.usi,) else ()
                labels = _kif_pv(before, record.usi, pv)
                lines.append(" ".join([f"*評価値 {value:+d}", *labels]))
        if record.actor is Actor.HUMAN and mark_human_moves:
            lines.append(KIF_HUMAN_MARK)
    side = Color.BLACK if board.turn == cshogi.BLACK else Color.WHITE
    word, outcome = _kif_end(info, side)
    if word is not None:
        lines.append(f"{number + 1:4d} {word}")
        lines.append(f"まで{number}手で{outcome}")
    return "\n".join(lines) + "\n"


def _kif_pv(before, usi: str, pv: Sequence[str], limit: int = 12) -> list[str]:
    """読み筋（指した手の次から）を `△３四歩` 形式にする。非合法な手が出たらそこで打ち切る。"""
    work = before.copy()
    work.push_usi(usi)
    labels: list[str] = []
    previous = usi[2:4]
    for move_usi in pv[:limit]:
        try:
            move = work.move_from_usi(move_usi)
            if not move or not work.is_legal(move):
                break
        except Exception:
            break
        mark = "▲" if work.turn == cshogi.BLACK else "△"
        labels.append(mark + kif_move(work, move_usi, previous).split("(")[0])
        previous = move_usi[2:4]
        work.push(move)
    return labels


_FORMATS = {"csa": ("csa", "utf-8"), "kif": ("kif", "cp932"), "kifu": ("kifu", "utf-8")}


def _free_path(path: Path) -> Path:
    """path が空いていればそのまま、あれば `<名前>_2.<拡張子>` … と空きを探す（上書きしない）。"""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
    return candidate


def save(directory: Path, info: GameInfo, moves: Sequence[MoveRecord], *,
         formats: Sequence[str], mark_human_moves: bool, score_pov: str = "black") -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for format_ in formats:
        if format_ not in _FORMATS:
            continue
        ext, encoding = _FORMATS[format_]
        if format_ == "csa":
            text = render_csa(info, moves, mark_human_moves=mark_human_moves, score_pov=score_pov)
        else:
            text = render_kif(info, moves, mark_human_moves=mark_human_moves, score_pov=score_pov,
                              utf8_header=format_ == "kifu")
        path = _free_path(directory / kifu_filename(info, ext))
        path.write_bytes(text.encode(encoding, errors="replace"))
        written.append(path)
    return written
