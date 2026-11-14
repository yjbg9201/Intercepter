"""§7.2 のレース規則（SPEC §9 シナリオ 2, 3, 9, 18）。到着順 = キューへの投入順で再現する。"""

import cshogi

from rig import B, W, Rig
from takeover.core import events as ev
from takeover.core.types import Mode, Phase


async def test_bestmove_then_takeover_keeps_the_move(fake_clock):
    """シナリオ 3（bestmove が先）: 手は有効のまま、モードだけ MANUAL。"""
    rig = await Rig(fake_clock).start()
    rig.engines[B].release_bestmove("7g7f")
    rig.core.post(ev.SetModeCommand(color=B, mode=Mode.MANUAL))
    await rig.pump()
    assert rig.usi() == ["7g7f"] and rig.seats[B].mode is Mode.MANUAL
    assert any("送信済み" in n for n in rig.core.notices)
    await rig.engine_move(W, "3c3d")
    assert len(rig.searches(B)) == 1 and len(rig.snap.legal_moves) > 0  # 次の手番は人間


async def test_takeover_then_bestmove_discards_the_move(fake_clock):
    """シナリオ 3（奪取が先）: 後から処理される bestmove は search_id 不一致で破棄。"""
    rig = await Rig(fake_clock).start()
    rig.core.post(ev.SetModeCommand(color=B, mode=Mode.MANUAL))
    rig.engines[B].release_bestmove("7g7f")  # 奪取の処理より前にエンジンが返していた
    await rig.pump()
    assert rig.usi() == [] and rig.seats[B].mode is Mode.MANUAL
    assert len(rig.logged("commit")) == 0 and len(rig.logged("bestmove_discarded")) == 1
    assert len(rig.snap.legal_moves) == 30


async def test_stale_bestmove_from_previous_position_is_discarded(fake_clock):
    """過去不具合: 前の局面の手を指す。"""
    rig = await Rig(fake_clock).start()
    old = rig.engines[B].current_search_id
    await rig.take(B)
    await rig.release(B)  # 新しい探索が始まる
    rig.engines[B].emit_stale_bestmove(old, "2g2f")
    await rig.pump()
    assert rig.usi() == []
    await rig.engine_move(B, "7g7f")
    assert rig.usi() == ["7g7f"]


async def test_duplicate_bestmove_commits_once(fake_clock):
    rig = await Rig(fake_clock).start()
    sid = rig.engines[B].current_search_id
    rig.engines[B].release_bestmove("7g7f")
    rig.engines[B].emit_stale_bestmove(sid, "2g2f")  # 同じ search_id がもう一度
    await rig.pump()
    assert rig.usi() == ["7g7f"] and len(rig.logged("commit")) == 1


async def test_other_seats_bestmove_cannot_move_for_this_seat(fake_clock):
    rig = await Rig(fake_clock).start()
    rig.engines[W].emit_stale_bestmove(1, "7g7f")  # 後手のエンジンが先手番に bestmove
    await rig.pump()
    assert rig.usi() == []


async def test_human_move_rejections(fake_clock):
    """シナリオ 9: 非合法手・手番外・AUTO の席・1 手番 2 回目。"""
    rig = await Rig(fake_clock).start()
    await rig.human_move(B, "7g7f")  # AUTO の席
    assert rig.usi() == []
    await rig.take(B)
    await rig.human_move(B, "7g7e")  # 非合法
    await rig.human_move(B, "xyz")  # 壊れた入力
    await rig.human_move(W, "3c3d")  # 手番外（後手は AUTO でもある）
    assert rig.usi() == []
    # 2 台の端末から同じ画面（ply=0）で同時に着手
    rig.core.post(ev.MoveCommand(color=B, ply=0, usi="7g7f", client_id="pc"))
    rig.core.post(ev.MoveCommand(color=B, ply=0, usi="2g2f", client_id="phone"))
    await rig.pump()
    assert rig.usi() == ["7g7f"] and len(rig.logged("commit")) == 1
    assert len(rig.logged("command_rejected")) == 5


async def test_stale_ply_move_is_rejected_even_if_legal(fake_clock):
    """シナリオ 18: 相手の手を挟んで遅れて届いた着手は、次の手番で合法でも指されない。"""
    rig = await Rig(fake_clock).start()
    await rig.take(B)
    await rig.human_move(B, "2g2f")
    await rig.engine_move(W, "3c3d")
    assert rig.snap.ply == 2 and "7g7f" in rig.snap.legal_moves
    await rig.human_move(B, "7g7f", ply=0)  # 古い画面から
    assert rig.usi() == ["2g2f", "3c3d"]
    rig.core.post(ev.ResignCommand(color=B, ply=0))
    rig.core.post(ev.DeclareCommand(color=B, ply=1))
    await rig.pump()
    assert rig.snap.phase is Phase.PLAYING
    await rig.human_move(B, "7g7f")
    assert rig.usi() == ["2g2f", "3c3d", "7g7f"]


async def test_set_mode_is_idempotent_from_two_clients(fake_clock):
    rig = await Rig(fake_clock).start()
    for client in ("pc", "phone"):
        rig.core.post(ev.SetModeCommand(color=B, mode=Mode.MANUAL, client_id=client))
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL
    assert len(rig.engines[B].calls_of("cancel")) == 1 and len(rig.logged("mode_change")) == 1


async def test_random_full_game_keeps_invariants(fake_clock):
    """偽エンジン同士が終局まで指せる。全手が合法で、探索は常に 1 つ以下（EngineBusyError が出ない）。"""
    import random

    rng = random.Random(3)

    def reply(position_cmd):
        board = cshogi.Board()
        board.set_position(position_cmd[len("position "):])
        legal = sorted(cshogi.move_to_usi(m) for m in board.legal_moves)
        return (rng.choice(legal), None) if legal else ("resign", None)

    rig = await Rig(fake_clock, auto_reply=reply, max_moves=80).start()
    assert rig.snap.phase is Phase.ENDED
    board = cshogi.Board()
    for usi in rig.usi():
        assert board.is_legal(board.move_from_usi(usi))
        board.push_usi(usi)
    assert len(rig.logged("commit")) >= len(rig.usi()) > 10
    plies = [r["ply"] for r in rig.logged("commit") if "ply" in r]
    assert plies == sorted(set(plies))  # 1 手番 1 コミット
