"""WebSocket メッセージ（JSON）⇔ コマンド／スナップショットの変換（純関数。I/O なし）。

参照: SPEC §6.7「WebSocket メッセージ」

- UI→サーバ: move {color, ply, usi} / set_mode {color, mode} / resign {color, ply} /
  declare {color, ply} / set_hint {enabled}
- サーバ→UI: snapshot / engine_info / notice / hello
- 壊れた入力・未知の type・型の違う項目は例外にせず None を返す（呼び出し側が notice で知らせる）。
- 評価値を「見る側の視点」に直すのはここ（Score は探索時の手番側＝指した側の視点で届く）。
  視点は CSA モードなら自分の色、ローカルモードなら先手。
- 秘密情報（trip / password / UI トークン）を出力に含めない。
"""

from __future__ import annotations

import json
from typing import Any

import cshogi

from ..core import events as ev
from ..core.snapshot import Snapshot
from ..core.types import Color, Mode, Score

_FILES = "０１２３４５６７８９"
_RANKS = "〇一二三四五六七八九"
_DROP_NAMES = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛"}
_COLORS = {"b": Color.BLACK, "w": Color.WHITE}
_MODES = {"auto": Mode.AUTO, "manual": Mode.MANUAL}
MAX_MESSAGE_BYTES = 4096


def parse_client_message(raw: str, *, client_id: str) -> ev.Command | None:
    """UI から届いた JSON 文字列を Command にする。解釈できなければ None。"""
    if not isinstance(raw, str) or len(raw) > MAX_MESSAGE_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    color = _COLORS.get(data.get("color")) if isinstance(data.get("color"), str) else None
    ply = data.get("ply")
    ply_ok = isinstance(ply, int) and not isinstance(ply, bool) and ply >= 0
    if kind == "move":
        usi = data.get("usi")
        if color is None or not ply_ok or not isinstance(usi, str) or not (4 <= len(usi) <= 5):
            return None
        return ev.MoveCommand(color=color, ply=ply, usi=usi, client_id=client_id)
    if kind == "set_mode":
        mode = _MODES.get(data.get("mode")) if isinstance(data.get("mode"), str) else None
        if color is None or mode is None:
            return None
        return ev.SetModeCommand(color=color, mode=mode, client_id=client_id)
    if kind in ("resign", "declare"):
        if color is None or not ply_ok:
            return None
        cls = ev.ResignCommand if kind == "resign" else ev.DeclareCommand
        return cls(color=color, ply=ply, client_id=client_id)
    if kind == "set_hint":
        if not isinstance(data.get("enabled"), bool):
            return None
        return ev.SetHintCommand(enabled=data["enabled"], client_id=client_id)
    return None


# ---- 表記 ----------------------------------------------------------------- #


def move_label(board, usi: str, previous_to: str | None = None) -> str:
    """`▲７六歩` 形式の表記。board は指す前の局面（変更しない）。作れなければ USI のまま返す。"""
    try:
        mark = "▲" if board.turn == cshogi.BLACK else "△"
        dest = usi[2:4]
        file_, rank = int(dest[0]), ord(dest[1]) - ord("a") + 1
        where = "同" if previous_to == dest else _FILES[file_] + _RANKS[rank]
        if usi[1] == "*":
            return f"{mark}{where}{_DROP_NAMES[usi[0]]}打"
        src_file, src_rank = int(usi[0]), ord(usi[1]) - ord("a") + 1
        piece = cshogi.PIECE_JAPANESE_SYMBOLS[board.piece_type((src_file - 1) * 9 + (src_rank - 1))]
        return f"{mark}{where}{piece}{'成' if usi.endswith('+') else ''}"
    except (ValueError, IndexError, KeyError):
        return usi


def pv_labels(sfen: str, pv: tuple[str, ...], *, limit: int = 12) -> list[str]:
    """読み筋を sfen の局面から順に表記にする。非合法な手が出たら、そこから先は USI のまま。"""
    try:
        board = cshogi.Board(sfen=sfen)
    except Exception:
        return list(pv[:limit])
    labels: list[str] = []
    previous = None
    for index, usi in enumerate(pv[:limit]):
        try:
            move = board.move_from_usi(usi)
            ok = bool(move) and board.is_legal(move)
        except Exception:
            ok = False
        if not ok:
            labels.extend(pv[index:limit])
            break
        labels.append(move_label(board, usi, previous))
        previous = usi[2:4]
        board.push(move)
    return labels


def _score_dict(score: Score | None, *, flip: bool) -> dict[str, Any] | None:
    if score is None or (score.cp is None and score.mate is None):
        return None
    sign = -1 if flip else 1
    return {
        "cp": None if score.cp is None else sign * score.cp,
        "mate": None if score.mate is None else sign * score.mate,
        "bound": score.bound,
    }


def viewpoint(snapshot: Snapshot) -> Color:
    return snapshot.my_color or Color.BLACK


def snapshot_message(snapshot: Snapshot) -> dict[str, Any]:
    """`{"type": "snapshot", ...}`。"""
    view = viewpoint(snapshot)
    board = cshogi.Board(sfen=snapshot.start_sfen) if snapshot.start_sfen else cshogi.Board()
    for usi in snapshot.start_moves:
        board.push_usi(usi)
    moves = []
    previous = None
    for record in snapshot.moves:
        label = move_label(board, record.usi, previous)
        previous = record.usi[2:4]
        try:
            board.push_usi(record.usi)
        except Exception:
            pass
        moves.append({
            "ply": record.ply, "usi": record.usi, "label": label, "color": record.color.value,
            "actor": record.actor.value, "elapsed_ms": record.elapsed_ms,
            "score": _score_dict(record.score, flip=record.color is not view),
        })
    clock = None
    if snapshot.clock is not None:
        c = snapshot.clock
        clock = {
            "remaining_ms": {color.value: ms for color, ms in c.remaining_ms.items()},
            "byoyomi_ms": c.byoyomi_ms, "increment_ms": c.increment_ms,
            "running": c.running.value if c.running else None, "turn_elapsed_ms": c.turn_elapsed_ms,
        }
    return {
        "type": "snapshot",
        "rev": snapshot.rev,
        "phase": snapshot.phase.value,
        "game_id": snapshot.game_id,
        "ply": snapshot.ply,
        "sfen": snapshot.sfen,
        "turn": snapshot.turn.value,
        "my_color": snapshot.my_color.value if snapshot.my_color else None,
        "view": view.value,
        "seats": {
            color.value: {
                "kind": seat.kind.value, "name": seat.name,
                "mode": seat.mode.value if seat.mode else None,
                "has_engine": seat.has_engine, "engine_state": seat.engine_state,
            }
            for color, seat in snapshot.seats.items()
        },
        "clock": clock,
        "moves": moves,
        "last_move": snapshot.last_move,
        "legal_moves": list(snapshot.legal_moves),
        "can_declare": snapshot.can_declare,
        "hint_enabled": snapshot.hint_enabled,
        "result": snapshot.result.value if snapshot.result else None,
        "end_reason": snapshot.end_reason.value if snapshot.end_reason else None,
    }


def engine_info_message(color: Color, kind: str, info: ev.EngineInfoEvent, *, view: Color,
                        sfen: str | None = None) -> dict[str, Any]:
    """`{"type": "engine_info", ...}`。kind は think / ponder / hint。

    評価値はその席（color）のエンジンが出した値＝ color 視点なので、見る側（view）に合わせる。
    sfen（現在の局面）があれば読み筋を表記にする（ponder 中は局面が違うので USI のままになる）。
    """
    return {
        "type": "engine_info", "color": color.value, "kind": kind, "depth": info.depth,
        "seldepth": info.seldepth, "score": _score_dict(info.score, flip=color is not view),
        "nodes": info.nodes, "nps": info.nps, "hashfull": info.hashfull, "multipv": info.multipv,
        "pv": list(info.pv[:12]),
        "pv_labels": pv_labels(sfen, info.pv) if sfen and kind != "ponder" else list(info.pv[:12]),
    }


def notice_message(text: str) -> dict[str, Any]:
    return {"type": "notice", "text": text}


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
