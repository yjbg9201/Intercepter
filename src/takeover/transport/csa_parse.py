"""CSA プロトコルの行の解析・生成（純関数。I/O なし）。

参照: SPEC §6.5、§6.4（時間の単位）、§12（評価値の符号・mate の写像は未決）

指し手の CSA ⇔ USI 変換は盤面が必要なので、cshogi.Board を引数に取る。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import cshogi
import cshogi.CSA

from ..core.types import Color, EndReason, Score, TimeControl

HIRATE_SFEN = "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1"
MATE_SCORE = 100000  # score mate N → ±(MATE_SCORE − N)。具体値は SPEC §12 の未決事項（仮）


class CsaParseError(Exception):
    pass


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


@dataclass(frozen=True, slots=True)
class EndLine:
    """`#` で始まる行。reason か result のどちらか一方が入る。"""

    reason: EndReason | None = None
    result: str | None = None  # "win" / "lose" / "draw" / "censored" / "chudan"
    raw: str = ""


def color_of(sign: str) -> Color:
    return Color.BLACK if sign == "+" else Color.WHITE


def sign_of(color: Color) -> str:
    return "+" if color is Color.BLACK else "-"


def build_login(name: str, password: str, *, x1: bool = False) -> str:
    """`LOGIN <name> <password>[ x1]`。"""
    return f"LOGIN {name} {password}" + (" x1" if x1 else "")


def build_floodgate_password(game: str, trip: str) -> str:
    """`<ゲーム名>,<識別文字列>`。"""
    return f"{game},{trip}"


def mask_login(line: str) -> str:
    """ログ用に LOGIN 行のパスワード部分を伏せる（I10）。LOGIN 行以外はそのまま返す。"""
    tokens = line.split(" ")
    if len(tokens) >= 3 and tokens[0] == "LOGIN":
        tokens[2] = "***"
        return " ".join(tokens)
    return line


_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(msec|sec|min)$")
_UNIT_MS = {"msec": 1, "sec": 1000, "min": 60000}


def parse_time_unit(text: str) -> int:
    """`1sec` / `1min` / `1msec` / `0.1sec` → ms。解釈できなければ 1000。"""
    match = _UNIT_RE.match(text.strip())
    if not match:
        return 1000
    return max(1, int(round(float(match.group(1)) * _UNIT_MS[match.group(2)])))


def _time_control(values: dict[str, str]) -> TimeControl:
    unit = parse_time_unit(values.get("Time_Unit", "1sec"))

    def get(key: str) -> int:
        try:
            return int(values.get(key, "0")) * unit
        except ValueError:
            return 0

    return TimeControl(
        total_ms=get("Total_Time"),
        byoyomi_ms=get("Byoyomi"),
        increment_ms=get("Increment"),
        least_time_per_move_ms=get("Least_Time_Per_Move"),
        time_unit_ms=unit,
    )


_KNOWN_KEYS = {
    "Protocol_Version", "Protocol_Mode", "Format", "Declaration", "Game_ID", "Name+", "Name-",
    "Your_Turn", "Rematch_On_Draw", "To_Move", "Max_Moves",
}
_TIME_KEYS = {
    "Time_Unit", "Total_Time", "Byoyomi", "Increment", "Least_Time_Per_Move", "Time_Roundup",
    "Remaining_Time+", "Remaining_Time-", "Last_Move", "Current_Turn", "Delay",
}


def parse_game_summary(lines: list[str]) -> GameSummary:
    """BEGIN Game_Summary 〜 END Game_Summary の行を解析する（BEGIN / END 行は含んでいてもよい）。"""
    top: dict[str, str] = {}
    times: dict[str, dict[str, str]] = {}
    position: list[str] = []
    unknown: dict[str, str] = {}
    block: str | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("BEGIN "):
            name = line[6:].strip()
            block = None if name == "Game_Summary" else name
            if block and block.startswith("Time"):
                times.setdefault(block, {})
            continue
        if line.startswith("END "):
            block = None
            continue
        if block == "Position":
            position.append(line)
            continue
        if ":" not in line:
            unknown[line] = ""
            continue
        key, value = line.split(":", 1)
        if block and block.startswith("Time"):
            times[block][key] = value
            if key not in _TIME_KEYS:
                unknown[f"{block}.{key}"] = value
        else:
            top[key] = value
            if key not in _KNOWN_KEYS:
                unknown[key] = value

    for required in ("Game_ID", "Your_Turn"):
        if required not in top:
            raise CsaParseError(f"Game_Summary に {required} がありません")

    common = times.get("Time", {})
    black_tc = _time_control(times.get("Time+", common))
    white_tc = _time_control(times.get("Time-", common))

    start_sfen: str | None = None
    start_moves: tuple[str, ...] = ()
    if position:
        text = "\n".join(l for l in position if not l.startswith("'")) + "\n"
        try:
            parsed = cshogi.CSA.Parser.parse_str(text)[0]
        except Exception as exc:  # cshogi の解析失敗は種類を問わず同じ扱い
            raise CsaParseError(f"Game_Summary の Position を解析できません: {exc}") from None
        if parsed.sfen != HIRATE_SFEN:
            start_sfen = parsed.sfen
        start_moves = tuple(cshogi.move_to_usi(m) for m in parsed.moves)

    max_moves = None
    if top.get("Max_Moves", "").isdigit():
        max_moves = int(top["Max_Moves"]) or None

    return GameSummary(
        game_id=top["Game_ID"],
        black_name=top.get("Name+", ""),
        white_name=top.get("Name-", ""),
        my_color=color_of(top["Your_Turn"].strip()),
        to_move=color_of(top.get("To_Move", "+").strip()),
        time_control={Color.BLACK: black_tc, Color.WHITE: white_tc},
        start_sfen=start_sfen,
        start_moves=start_moves,
        max_moves=max_moves,
        declaration=top.get("Declaration"),
        unknown=unknown,
    )


_MOVE_RE = re.compile(r"^([+-])(\d{4}[A-Z]{2})$")
_SPECIAL_RE = re.compile(r"^%[A-Z_]+$")


def parse_server_move(line: str) -> ServerMove | None:
    """`+7776FU,T12` / `%TORYO,T3` を解析する。指し手行でなければ None。

    `%` で始まる行の color は分からないので BLACK を入れておく（呼び出し側は手番から決める）。
    """
    parts = line.strip().split(",")
    head = parts[0]
    if not (_MOVE_RE.match(head) or _SPECIAL_RE.match(head)):
        return None
    elapsed = None
    comment = None
    for index, part in enumerate(parts[1:], 1):
        if part.startswith("T") and part[1:].isdigit() and elapsed is None:
            elapsed = int(part[1:])
        elif part.startswith("'"):
            comment = ",".join(parts[index:])
            break
    color = color_of(head[0]) if head[0] in "+-" else Color.BLACK
    return ServerMove(color=color, csa=head, elapsed_units=elapsed, comment=comment)


_REASONS = {
    "#RESIGN": EndReason.RESIGN,
    "#TIME_UP": EndReason.TIME_UP,
    "#ILLEGAL_MOVE": EndReason.ILLEGAL_MOVE,
    "#ILLEGAL_ACTION": EndReason.ILLEGAL_MOVE,
    "#SENNICHITE": EndReason.SENNICHITE,
    "#OUTE_SENNICHITE": EndReason.OUTE_SENNICHITE,
    "#JISHOGI": EndReason.JISHOGI,
    "#MAX_MOVES": EndReason.MAX_MOVES,
    "#ABNORMAL": EndReason.DISCONNECTED,
}
_RESULTS = {"#WIN": "win", "#LOSE": "lose", "#DRAW": "draw", "#CENSORED": "censored", "#CHUDAN": "chudan"}


def parse_end_line(line: str) -> EndLine | None:
    """`#RESIGN` などの理由行は reason、`#WIN` / `#LOSE` / `#DRAW` / `#CENSORED` / `#CHUDAN` は result。

    `#` で始まらない行は None。未知の `#` 行は reason=UNKNOWN。
    """
    text = line.strip()
    if not text.startswith("#"):
        return None
    if text in _RESULTS:
        return EndLine(result=_RESULTS[text], raw=text)
    return EndLine(reason=_REASONS.get(text, EndReason.UNKNOWN), raw=text)


def usi_to_csa(board, usi: str) -> str:
    """USI の指し手を、board（指す前の局面）での CSA 表記 `+7776FU` にする。"""
    move = board.move_from_usi(usi)
    sign = "+" if board.turn == cshogi.BLACK else "-"
    return sign + cshogi.move_to_csa(move)


def csa_to_usi(board, csa: str) -> str:
    """`+7776FU`（符号はあってもなくてもよい）を USI にする。board は指す前の局面。"""
    body = csa[1:] if csa[:1] in "+-" else csa
    return cshogi.move_to_usi(board.move_from_csa(body))


def pv_to_csa(board, pv: tuple[str, ...], *, limit: int = 32) -> tuple[str, ...]:
    """読み筋（USI）を board から順に進めて CSA 表記にする。非合法な手が出たらそこで打ち切る。

    board は変更しない（複製して進める）。
    """
    work = board.copy()
    result: list[str] = []
    for usi in pv[:limit]:
        try:
            move = work.move_from_usi(usi)
        except Exception:
            break
        if not move or not work.is_legal(move):
            break
        sign = "+" if work.turn == cshogi.BLACK else "-"
        result.append(sign + cshogi.move_to_csa(move))
        work.push(move)
    return tuple(result)


def score_to_int(score: Score) -> int | None:
    """Score を 1 つの整数にする（視点は変えない）。mate は ±(MATE_SCORE − N)。"""
    if score.mate is not None:
        n = abs(score.mate)
        return (MATE_SCORE - n) if score.mate > 0 else -(MATE_SCORE - n)
    return score.cp


def comment_score(score: Score, mover: Color, pov: str) -> int | None:
    """評価値コメントに書く整数。pov は "mover"（指した側視点）/ "black"（先手視点）。

    score は「指した側」視点で渡す。符号の規約はこの関数だけに閉じ込める（SPEC §6.5、§12）。
    """
    value = score_to_int(score)
    if value is None:
        return None
    if pov == "black" and mover is Color.WHITE:
        return -value
    return value


def comment_to_mover_score(value: int, mover: Color, pov: str) -> int:
    """受け取ったコメントの評価値を「指した側」視点に直す（comment_score の逆）。"""
    if pov == "black" and mover is Color.WHITE:
        return -value
    return value


_COMMENT_RE = re.compile(r"^'\*\s*(-?\d+)\s*(.*)$")


def parse_comment(comment: str) -> tuple[int, tuple[str, ...]] | None:
    """`'* 30 -3334FU +2726FU` → (30, ("-3334FU", "+2726FU"))。形式が違えば None。"""
    match = _COMMENT_RE.match(comment.strip())
    if not match:
        return None
    return int(match.group(1)), tuple(match.group(2).split())


def build_move_line(csa: str, *, score: int | None = None, pv_csa: tuple[str, ...] = (),
                    comment: str = "") -> str:
    """送信する指し手行。score があれば `,'* <評価値> <読み筋>` を付ける。

    人間の手では score / pv を渡さない（コメントなしの行になる）。
    comment（clean_comment 済み。棋譜に残したい形の行を改行で並べたもの）があれば、評価値の代わりにそれを付ける。
    """
    if comment:
        return ",".join([csa, *comment_wire_segments(comment)])
    if score is None:
        return csa
    parts = ["'*", str(score), *pv_csa]
    return f"{csa},{' '.join(parts)}"


MAX_RAW_LINE = 200


def clean_line(text: str) -> str:
    """通信欄の入力を 1 行にする: 制御文字（改行を含む）を除き、前後の空白を落とし、長さを切る。"""
    cleaned = "".join(ch for ch in text if ch >= " " and ch != "\x7f").strip()
    return cleaned[:MAX_RAW_LINE]


MAX_COMMENT_LINES = 8


def clean_comment(text: str) -> str:
    """指し手に付ける自由コメントを「サーバの棋譜に残したい形」にそろえる。複数行は改行区切り。空なら ""。

    1 行ごとに:
    - `'*` で始まればそのまま（例: `'** 31511 -5251OU`＝評価値コメントの形、`'* メモ`＝フリーコメントの形）。
    - `'` だけで始まれば `'*` に直す。`'` で始まらなければ `'* ` を補う。
    - サーバは指し手行を `,` で区切るので、`,` は `、` に置き換える。
    """
    lines: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = clean_line(raw).replace(",", "、")
        if not line or line in ("'", "'*"):
            continue
        if line.startswith("'*"):
            pass
        elif line.startswith("'"):
            line = "'*" + line[1:]
        else:
            line = "'* " + line
        lines.append(line)
    return "\n".join(lines[:MAX_COMMENT_LINES])


def comment_wire_segments(comment: str) -> list[str]:
    """clean_comment 済みのコメントを、指し手行に `,` で続ける区切りの列にする。

    shogi-server は、指し手の次の区切りが `'X` なら棋譜に `'*X` と書く（`*` を 1 つ足す。command.rb の MoveCommand）。
    そのため 1 つ目は `*` を 1 つ減らして送る: 残したい `'** 30 …` → 送る `'* 30 …`、残したい `'* メモ` → 送る `' メモ`。
    2 つ目以降はそのままの形で送る（公開されているソースでは 2 つ目以降は棋譜に書かれない。SPEC §6.11）。
    """
    lines = [line for line in comment.split("\n") if line]
    if not lines:
        return []
    return ["'" + lines[0][2:], *lines[1:]]


def classify_raw_line(line: str) -> str:
    """通信欄の 1 行（clean_line 済み）の種類。SPEC §6.11 の振り分け表と 1 対 1。

    - "empty"    : 空
    - "login"    : LOGIN（秘密を含むので通信欄からは送らせない）
    - "logout"   : LOGOUT（対局待ちの切断として扱う）
    - "auto"     : AGREE / REJECT（アプリが自動で送る）
    - "move"     : `+` / `-` で始まる行（指し手。盤から指す）
    - "resign"   : %TORYO
    - "declare"  : %KACHI
    - "special"  : それ以外の `%X`（サーバは指し手として扱う。手番外に送ると保留されて次の手番に適用される）
    - "comment"  : `'` で始まる行（次の自分の手に付ける）
    - "extended" : `%%` で始まる行（拡張コマンド。対局の進行に影響しない）
    - "other"    : それ以外（そのまま送る。サーバは `##[ERROR]` を返すだけ）
    """
    if not line:
        return "empty"
    upper = line.upper()
    if upper.startswith("LOGIN"):
        return "login"
    if upper.startswith("LOGOUT"):
        return "logout"
    if upper.startswith(("AGREE", "REJECT")):
        return "auto"
    if line[0] in "+-":
        return "move"
    if line.startswith("%%"):
        return "extended"
    if line.startswith("%"):
        head = upper.split(",")[0].strip()
        return {"%TORYO": "resign", "%KACHI": "declare"}.get(head, "special")
    if line.startswith("'"):
        return "comment"
    return "other"
