import json

import cshogi

from rig import B, W, Rig
from takeover.core import events as ev
from takeover.core.types import Color, Mode, Score
from takeover.ui import messages as m


def parse(obj):
    return m.parse_client_message(json.dumps(obj), client_id="c1")


def test_parse_valid_commands():
    assert parse({"type": "move", "color": "b", "ply": 3, "usi": "7g7f"}) == ev.MoveCommand(
        color=Color.BLACK, ply=3, usi="7g7f", client_id="c1")
    assert parse({"type": "set_mode", "color": "w", "mode": "manual"}) == ev.SetModeCommand(
        color=Color.WHITE, mode=Mode.MANUAL, client_id="c1")
    assert parse({"type": "resign", "color": "b", "ply": 0}) == ev.ResignCommand(color=Color.BLACK, ply=0, client_id="c1")
    assert parse({"type": "declare", "color": "w", "ply": 9}) == ev.DeclareCommand(color=Color.WHITE, ply=9, client_id="c1")
    assert parse({"type": "set_hint", "enabled": True}) == ev.SetHintCommand(enabled=True, client_id="c1")


def test_parse_rejects_garbage_without_raising():
    bad = [
        {"type": "move", "color": "b", "ply": "3", "usi": "7g7f"}, {"type": "move", "color": "x", "ply": 1, "usi": "7g7f"},
        {"type": "move", "color": "b", "ply": -1, "usi": "7g7f"}, {"type": "move", "color": "b", "ply": True, "usi": "7g7f"},
        {"type": "move", "color": "b", "ply": 1, "usi": 7}, {"type": "move", "color": "b", "ply": 1, "usi": "x" * 9},
        {"type": "set_mode", "color": "b", "mode": "toggle"}, {"type": "set_mode", "color": ["b"], "mode": "auto"},
        {"type": "resign"}, {"type": "set_hint", "enabled": "yes"}, {"type": "shutdown"}, [], "x", 1, None,
    ]
    assert all(parse(b) is None for b in bad)
    assert m.parse_client_message("{not json", client_id="c") is None
    assert m.parse_client_message("x" * 10_000, client_id="c") is None


def test_move_labels():
    board = cshogi.Board()
    assert m.move_label(board, "7g7f") == "▲７六歩"
    board.push_usi("7g7f")
    assert m.move_label(board, "3c3d") == "△３四歩"
    board.push_usi("3c3d")
    assert m.move_label(board, "8h2b+") == "▲２二角成"
    board.push_usi("8h2b+")
    assert m.move_label(board, "3a2b", previous_to="2b") == "△同銀"
    board.push_usi("3a2b")
    assert m.move_label(board, "B*5e") == "▲５五角打"
    assert m.move_label(board, "zz") == "zz"
    assert m.pv_labels(cshogi.Board().sfen(), ("7g7f", "3c3d", "9z9z", "2g2f")) == ["▲７六歩", "△３四歩", "9z9z", "2g2f"]


async def test_snapshot_message_is_json_and_scores_follow_viewpoint(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.engine_move(B, "7g7f", score=Score(cp=50), pv=("7g7f", "3c3d"))
    await rig.engine_move(W, "3c3d", score=Score(cp=80))
    await rig.take(B)
    msg = m.snapshot_message(rig.snap)
    text = m.dumps(msg)
    data = json.loads(text)
    assert data["type"] == "snapshot" and data["ply"] == 2 and data["view"] == "b"
    assert [x["label"] for x in data["moves"]] == ["▲７六歩", "△３四歩"]
    assert [x["score"]["cp"] for x in data["moves"]] == [50, -80]  # 後手の +80 は先手視点で -80
    assert data["seats"]["b"]["mode"] == "manual" and data["seats"]["w"]["kind"] == "local"
    assert len(data["legal_moves"]) > 30 and data["clock"]["running"] == "b"
    assert data["clock"]["remaining_ms"] == {"b": 310000, "w": 310000}


def test_engine_info_message_viewpoint():
    info = ev.EngineInfoEvent(color=Color.WHITE, search_id=1, depth=9, score=Score(mate=3), pv=("3c3d",))
    msg = m.engine_info_message(Color.WHITE, "think", info, view=Color.BLACK)
    assert msg["score"]["mate"] == -3 and msg["color"] == "w" and msg["pv"] == ["3c3d"]
    assert m.engine_info_message(Color.WHITE, "think", info, view=Color.WHITE)["score"]["mate"] == 3
