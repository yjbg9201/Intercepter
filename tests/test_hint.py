"""ヒント探索（SPEC §6.9、I11、§9 シナリオ 16）。"""

from rig import B, W, Rig
from takeover.core import events as ev
from takeover.core.game import GamePolicy
from takeover.core.types import Mode
from takeover.engine.base import EngineState

HINT = GamePolicy(hint_enabled=True, hint_multipv=3)


async def test_hint_starts_on_manual_turn_and_its_bestmove_is_never_committed(fake_clock):
    rig = await Rig(fake_clock, policy=HINT).start()
    await rig.take(B)
    engine = rig.engines[B]
    assert [c.method for c in engine.calls][-3:] == ["cancel", "set_options", "analyze"]
    assert engine.options["MultiPV"] == 3 and engine.state is EngineState.ANALYZING
    hint_id = engine.current_search_id
    engine.emit_stale_bestmove(hint_id, "7g7f")  # ヒント探索の bestmove が届いても
    await rig.pump()
    assert rig.usi() == [] and rig.logged("bestmove_discarded")[-1]["reason"] == "hint"
    assert not rig.logged("commit")


async def test_human_move_cancels_hint_and_no_hint_in_opponent_turn(fake_clock):
    rig = await Rig(fake_clock, policy=HINT).start()
    await rig.take(B)
    await rig.human_move(B, "7g7f")
    assert rig.engines[B].last_call().method == "cancel" and rig.engines[B].state is EngineState.IDLE
    await rig.engine_move(W, "3c3d")
    assert rig.engines[B].last_call("analyze").args["position"] == "position startpos moves 7g7f 3c3d"


async def test_release_cancels_hint_and_restores_multipv_before_go(fake_clock):
    rig = await Rig(fake_clock, policy=HINT).start()
    await rig.take(B)
    await rig.release(B)
    assert [c.method for c in rig.engines[B].calls][-3:] == ["cancel", "set_options", "think"]
    assert rig.engines[B].options["MultiPV"] == 1
    await rig.engine_move(B, "7g7f")
    assert rig.usi() == ["7g7f"]


async def test_hint_toggle_and_game_end(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.take(B)
    assert rig.engines[B].calls_of("analyze") == []
    rig.core.post(ev.SetHintCommand(enabled=True))
    await rig.pump()
    assert rig.engines[B].state is EngineState.ANALYZING and rig.snap.hint_enabled
    rig.core.post(ev.SetHintCommand(enabled=False))
    await rig.pump()
    assert rig.engines[B].state is EngineState.IDLE
    rig.core.post(ev.SetHintCommand(enabled=True))
    rig.core.post(ev.ResignCommand(color=B, ply=0))
    await rig.pump()
    assert rig.engines[B].state is EngineState.IDLE and rig.core.result is not None


async def test_schedule_makes_opening_moves_manual(fake_clock):
    """予約切替（SPEC §7.3）。"""
    rig = await Rig(fake_clock, policy=GamePolicy(schedule=((1, 3),))).start()
    assert rig.seats[B].mode is Mode.MANUAL and rig.searches(B) == []
    await rig.human_move(B, "7g7f")
    assert rig.seats[W].mode is Mode.MANUAL  # 2 手目も範囲内
    await rig.human_move(W, "3c3d")
    await rig.human_move(B, "2g2f")
    assert rig.seats[W].mode is Mode.AUTO and len(rig.searches(W)) == 1  # 4 手目で範囲を抜けた
    await rig.engine_move(W, "8c8d")
    assert rig.seats[B].mode is Mode.AUTO and len(rig.searches(B)) == 1
