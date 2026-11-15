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


# ---- ヒントの MultiPV の切替と、ヒントの読み筋をコメントとして付ける設定（2026-11-16 追加。SPEC §6.9） ----


async def test_hint_multipv_can_be_switched_off_and_on_during_hint(fake_clock):
    rig = await Rig(fake_clock, policy=HINT).start()
    await rig.take(B)
    engine = rig.engines[B]
    assert engine.options["MultiPV"] == 3 and rig.snap.hint_multipv == 3 and rig.snap.hint_multipv_max == 3
    rig.core.post(ev.SetHintMultiPvCommand(enabled=False))
    await rig.pump()
    # 探索を止めてから MultiPV を 1 にし、同じ局面でヒント探索をやり直す（IDLE でしか変えられない）
    assert [c.method for c in engine.calls][-3:] == ["cancel", "set_options", "analyze"]
    assert engine.options["MultiPV"] == 1 and engine.state is EngineState.ANALYZING and rig.snap.hint_multipv == 1
    rig.core.post(ev.SetHintMultiPvCommand(enabled=True))
    await rig.pump()
    assert engine.options["MultiPV"] == 3 and engine.state is EngineState.ANALYZING and rig.snap.hint_multipv == 3


async def test_hint_multipv_switch_while_not_hinting_touches_no_engine(fake_clock):
    rig = await Rig(fake_clock, policy=HINT).start()
    calls = len(rig.engines[B].calls)
    rig.core.post(ev.SetHintMultiPvCommand(enabled=False))
    await rig.pump()
    assert len(rig.engines[B].calls) == calls and rig.snap.hint_multipv == 1  # AUTO の思考には触らない
    await rig.take(B)
    assert [c.method for c in rig.engines[B].calls][-2:] == ["cancel", "analyze"]  # MultiPV は 1 のまま
    # hint.multipv = 1 の設定では、オンにしても 1 のまま
    rig2 = await Rig(fake_clock, policy=GamePolicy(hint_enabled=True)).start()
    rig2.core.post(ev.SetHintMultiPvCommand(enabled=True))
    await rig2.pump()
    assert rig2.snap.hint_multipv == 1 and rig2.snap.hint_multipv_max == 1


def _spy_sends(rig):
    sent = []
    original = rig.referee.send_move

    async def send_move(color, usi, **kwargs):
        sent.append((usi, kwargs.get("score"), tuple(kwargs.get("pv", ()))))
        await original(color, usi, **kwargs)

    rig.referee.send_move = send_move
    return sent


async def test_human_move_carries_hint_line_only_when_option_is_on(fake_clock):
    from takeover.core.types import Score

    async def play(policy):
        rig = await Rig(fake_clock, policy=policy).start()
        sent = _spy_sends(rig)
        await rig.take(B)
        engine = rig.engines[B]
        engine.emit_info(multipv=1, score=Score(cp=50), pv=("2g2f", "8c8d"))
        engine.emit_info(multipv=2, score=Score(cp=30), pv=("7g7f", "3c3d", "2g2f"))
        engine.emit_info(multipv=2, score=Score(cp=99, bound="lower"), pv=("7g7f",))  # 暫定値は使わない
        await rig.pump()
        await rig.human_move(B, "7g7f")
        return rig, sent

    rig, sent = await play(GamePolicy(hint_enabled=True, hint_multipv=3, hint_send_as_comment=True))
    assert sent == [("7g7f", Score(cp=30), ("7g7f", "3c3d", "2g2f"))]
    record = rig.core.moves[0]
    assert record.actor.value == "human" and record.score == Score(cp=30) and record.pv[0] == "7g7f"
    # 既定（オフ）では人間の手に評価値・読み筋を付けない
    rig, sent = await play(HINT)
    assert sent == [("7g7f", None, ())] and rig.core.moves[0].score is None


async def test_hint_comment_needs_a_matching_line_of_the_current_position(fake_clock):
    from takeover.core.types import Score

    rig = await Rig(fake_clock, policy=GamePolicy(hint_enabled=True, hint_send_as_comment=True)).start()
    sent = _spy_sends(rig)
    await rig.take(B)
    rig.engines[B].emit_info(multipv=1, score=Score(cp=50), pv=("2g2f", "8c8d"))
    await rig.pump()
    await rig.human_move(B, "7g7f")  # 候補にない手 → 何も付けない
    await rig.engine_move(W, "3c3d")
    await rig.human_move(B, "2g2f")  # 前の局面のヒント（2g2f）を持ち越さない
    assert [s[1:] for s in sent if s[0] in ("7g7f", "2g2f")] == [(None, ()), (None, ())]
