"""§7.1 の遷移表（SPEC §9 シナリオ 1, 4, 5 ほか）。スクリプト式エンジンで 1 行ずつ確かめる。"""

from rig import B, W, Rig
from takeover.core import events as ev
from takeover.core.game import GamePolicy
from takeover.core.types import Actor, EndReason, GameResult, Mode, Phase, TimeControl
from takeover.engine.base import EngineState

P0 = "position startpos"


async def test_start_engine_thinks_for_side_to_move(fake_clock):
    rig = await Rig(fake_clock).start()
    assert rig.snap.phase is Phase.PLAYING
    assert rig.searches(B) == [("think", P0)] and rig.searches(W) == []
    assert [c.method for c in rig.engines[B].calls][:2] == ["start", "new_game"]
    go = rig.engines[B].last_call("think").args["go_time"]
    assert (go.btime_ms, go.wtime_ms, go.binc_ms) == (299_000, 300_000, 10_000)  # 手番側だけ margin を引く


async def test_auto_bestmove_commits_and_opponent_thinks_with_full_move_list(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.engine_move(B, "7g7f")
    assert rig.usi() == ["7g7f"] and rig.actors() == ["engine"]
    assert rig.searches(W) == [("think", "position startpos moves 7g7f")]
    await rig.engine_move(W, "3c3d")
    assert rig.searches(B)[-1] == ("think", "position startpos moves 7g7f 3c3d")  # I4: 毎回全手順
    assert rig.core.moves[0].elapsed_ms == 0


async def test_takeover_while_thinking_cancels_and_offers_legal_moves(fake_clock):
    rig = await Rig(fake_clock).start()
    sid = rig.engines[B].current_search_id
    await rig.take(B)
    assert rig.engines[B].last_call().method == "cancel"
    assert rig.seats[B].mode is Mode.MANUAL and len(rig.snap.legal_moves) == 30
    rig.engines[B].emit_stale_bestmove(sid, "7g7f")  # キューに残っていた bestmove
    await rig.pump()
    assert rig.usi() == []
    assert rig.logged("bestmove_discarded")[0]["search_id"] == sid


async def test_human_move_then_release_in_opponent_turn_does_not_ponder(fake_clock):
    rig = await Rig(fake_clock, ponder=True).start()
    await rig.take(B)
    await rig.human_move(B, "7g7f")
    assert rig.actors() == ["human"] and rig.snap.legal_moves == ()
    await rig.release(B)  # 相手手番・MANUAL → 返却: AUTO にするだけ
    assert rig.seats[B].mode is Mode.AUTO and rig.searches(B) == [("think", P0)]
    await rig.engine_move(W, "3c3d")  # 相手の手が来たら go
    assert rig.searches(B)[-1] == ("think", "position startpos moves 7g7f 3c3d")


async def test_release_mid_turn_subtracts_elapsed_time(fake_clock):
    """シナリオ 4: 途中返却で btime が本手番の既経過分だけ減っている。"""
    rig = await Rig(fake_clock).start()
    await rig.take(B)
    fake_clock.advance(7_500)
    await rig.release(B)
    go = rig.engines[B].last_call("think").args["go_time"]
    assert (go.btime_ms, go.wtime_ms) == (300_000 - 7_500 - 1_000, 300_000)
    await rig.take(B)  # 返却直後の再奪取も cancel されるだけ
    assert rig.engines[B].state is EngineState.IDLE and rig.usi() == []


async def test_ponder_starts_after_echo_and_ponderhit(fake_clock):
    """シナリオ 1（ヒット）。"""
    rig = await Rig(fake_clock, ponder=True).start()
    await rig.engine_move(B, "7g7f", "3c3d")
    assert rig.searches(B)[-1] == ("ponder", "position startpos moves 7g7f 3c3d")
    go = rig.engines[B].last_call("ponder").args["go_time"]
    assert go.btime_ms == 300_000 + 10_000 - 1_000  # 加算後の残り − margin（既経過 0）
    ponder_id = rig.engines[B].current_search_id
    await rig.engine_move(W, "3c3d")
    assert rig.engines[B].last_call().method == "ponderhit"
    assert rig.engines[B].state is EngineState.THINKING
    await rig.engine_move(B, "2g2f")
    assert rig.usi() == ["7g7f", "3c3d", "2g2f"]
    assert rig.engines[B].calls_of("think") == rig.engines[B].calls_of("think")[:1]  # go は増えていない
    assert ponder_id == rig.engines[B].calls_of("ponder")[0].args["search_id"]


async def test_ponder_miss_cancels_and_thinks_on_real_position(fake_clock):
    """シナリオ 1（ミス）。"""
    rig = await Rig(fake_clock, ponder=True).start()
    await rig.engine_move(B, "7g7f", "3c3d")
    await rig.engine_move(W, "8c8d")
    methods = [c.method for c in rig.engines[B].calls][-2:]
    assert methods == ["cancel", "think"]
    assert rig.searches(B)[-1] == ("think", "position startpos moves 7g7f 8c8d")


async def test_takeover_during_ponder_cancels_and_next_turn_is_manual(fake_clock):
    rig = await Rig(fake_clock, ponder=True).start()
    await rig.engine_move(B, "7g7f", "3c3d")
    await rig.take(B)
    assert rig.engines[B].last_call().method == "cancel"
    assert any("次の手番" in n for n in rig.core.notices)
    await rig.engine_move(W, "3c3d")  # 予想手が当たっても MANUAL なので ponderhit しない
    assert rig.engines[B].calls_of("ponderhit") == []
    assert rig.engines[B].state is EngineState.IDLE and len(rig.snap.legal_moves) > 0


async def test_takeover_while_idle_in_opponent_turn_touches_nothing(fake_clock):
    """Q1: 相手手番・AUTO・待機で奪取 → MANUAL にするだけ。"""
    rig = await Rig(fake_clock).start()
    await rig.engine_move(B, "7g7f")
    before = len(rig.engines[B].calls)
    await rig.take(B)
    assert rig.seats[B].mode is Mode.MANUAL and len(rig.engines[B].calls) == before
    await rig.engine_move(W, "3c3d")
    assert rig.searches(B) == [("think", P0)] and len(rig.snap.legal_moves) > 0


async def test_game_end_during_ponder_cancels_and_sends_gameover(fake_clock):
    """シナリオ 5。"""
    rig = await Rig(fake_clock, ponder=True).start()
    await rig.engine_move(B, "7g7f", "3c3d")
    await rig.engine_move(W, "resign")
    assert rig.snap.phase is Phase.ENDED
    assert (rig.core.result, rig.core.end_reason) == (GameResult.BLACK_WIN, EndReason.RESIGN)
    assert [c.method for c in rig.engines[B].calls][-2:] == ["cancel", "game_over"]
    assert rig.engines[B].last_call("game_over").args["result"] == "win"
    assert rig.engines[W].last_call("game_over").args["result"] == "lose"
    assert rig.engines[B].state is EngineState.IDLE


async def test_human_resign_and_commands_after_end_are_rejected(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.take(B)
    rig.core.post(ev.ResignCommand(color=B, ply=0))
    await rig.pump()
    assert rig.core.result is GameResult.WHITE_WIN
    await rig.human_move(B, "7g7f", ply=0)
    await rig.take(W)
    assert rig.usi() == [] and rig.seats[W].mode is Mode.AUTO


async def test_human_only_seat_cannot_be_released(fake_clock):
    """Q2。"""
    rig = await Rig(fake_clock, engines=(False, True)).start()
    assert rig.seats[B].mode is Mode.MANUAL and len(rig.snap.legal_moves) == 30
    await rig.release(B)
    assert rig.seats[B].mode is Mode.MANUAL and any("エンジンがない" in n for n in rig.core.notices)
    await rig.human_move(B, "7g7f")
    assert rig.searches(W) == [("think", "position startpos moves 7g7f")]


async def test_engine_restart_then_rethink(fake_clock):
    """シナリオ 7（restart）。"""
    rig = await Rig(fake_clock).start()
    rig.engines[B].die("crash", will_restart=True)
    await rig.pump()
    assert rig.seats[B].mode is Mode.AUTO and len(rig.searches(B)) == 1
    fake_clock.advance(3_000)
    rig.engines[B].become_ready()
    await rig.pump()
    assert rig.searches(B)[-1] == ("think", P0) and len(rig.searches(B)) == 2
    assert rig.engines[B].last_call("think").args["go_time"].btime_ms == 300_000 - 3_000 - 1_000
    await rig.engine_move(B, "7g7f")
    assert rig.usi() == ["7g7f"]


async def test_engine_failure_falls_back_to_manual(fake_clock):
    """シナリオ 7（manual、および再起動できない場合）。"""
    rig = await Rig(fake_clock, policy=GamePolicy(on_engine_failure="manual")).start()
    rig.engines[B].die("crash", will_restart=True)
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL and len(rig.snap.legal_moves) == 30

    rig = await Rig(fake_clock).start()
    rig.engines[B].die("crash", will_restart=False)
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL
    await rig.release(B)  # DEAD のエンジンには返却できない（Q2）
    assert rig.seats[B].mode is Mode.MANUAL


async def test_takeover_while_engine_is_restarting(fake_clock):
    """Q3: 再起動中の奪取は即 MANUAL。再起動中の返却は AUTO にして、準備できたら go。"""
    rig = await Rig(fake_clock).start()
    rig.engines[B].die("crash", will_restart=True)
    await rig.pump()
    await rig.take(B)
    assert rig.seats[B].mode is Mode.MANUAL
    await rig.release(B)
    assert rig.seats[B].mode is Mode.AUTO and len(rig.searches(B)) == 1
    rig.engines[B].become_ready()
    await rig.pump()
    assert len(rig.searches(B)) == 2


async def test_illegal_bestmove_is_never_committed(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.engine_move(B, "7g7e")  # 非合法
    assert rig.usi() == [] and len(rig.searches(B)) == 2  # 1 回だけ考え直させる
    await rig.engine_move(B, "5i5a")
    assert rig.usi() == [] and rig.seats[B].mode is Mode.MANUAL
    assert len(rig.searches(B)) == 2


async def test_engine_win_claim_without_condition_is_not_sent(fake_clock):
    """Q8。"""
    rig = await Rig(fake_clock, policy=GamePolicy(on_engine_failure="manual")).start()
    await rig.engine_move(B, "win")
    assert rig.snap.phase is Phase.PLAYING and rig.seats[B].mode is Mode.MANUAL


WIN_SFEN = "RRBBK4/GGGGSS3/9/9/9/9/9/9/4k4 b 2P 1"


async def test_declaration_engine_and_human(fake_clock):
    """シナリオ 14 の GameCore 側: 条件成立なら宣言でき、不成立ならボタンも宣言も無効。"""
    rig = await Rig(fake_clock, start_sfen=WIN_SFEN).start()
    assert rig.searches(B)[0][1] == f"position sfen {WIN_SFEN}"
    await rig.engine_move(B, "win")
    assert (rig.core.result, rig.core.end_reason) == (GameResult.BLACK_WIN, EndReason.JISHOGI)

    rig = await Rig(fake_clock, start_sfen=WIN_SFEN).start()
    await rig.take(B)
    assert rig.snap.can_declare
    rig.core.post(ev.DeclareCommand(color=B, ply=0))
    await rig.pump()
    assert rig.core.end_reason is EndReason.JISHOGI

    rig = await Rig(fake_clock).start()
    await rig.take(B)
    assert not rig.snap.can_declare
    rig.core.post(ev.DeclareCommand(color=B, ply=0))
    await rig.pump()
    assert rig.snap.phase is Phase.PLAYING and any("条件" in n for n in rig.core.notices)


async def test_engine_resign_ask_switches_to_manual(fake_clock):
    rig = await Rig(fake_clock, policy=GamePolicy(on_engine_resign="ask")).start()
    await rig.engine_move(B, "resign")
    assert rig.snap.phase is Phase.PLAYING and rig.seats[B].mode is Mode.MANUAL
    await rig.human_move(B, "7g7f")
    assert rig.usi() == ["7g7f"]


async def test_time_up_in_local_mode(fake_clock):
    rig = await Rig(fake_clock, tc=TimeControl(total_ms=10_000)).start()
    fake_clock.advance(9_999)
    assert not rig.referee.check_flag()
    fake_clock.advance(1)
    assert rig.referee.check_flag()
    await rig.pump()
    assert (rig.core.result, rig.core.end_reason) == (GameResult.WHITE_WIN, EndReason.TIME_UP)
    assert rig.engines[B].last_call("cancel") is not None


async def test_remote_actor_never_appears_in_local_mode_and_snapshot_is_complete(fake_clock):
    rig = await Rig(fake_clock).start()
    await rig.engine_move(B, "7g7f", score=None)
    s = rig.snap
    assert s.ply == 1 and s.last_move == "7g7f" and s.turn is W and s.my_color is None
    assert s.seats[B].mode is Mode.AUTO and s.seats[B].engine_state == "idle"
    assert s.clock.remaining_ms[B] == 310_000 and s.clock.running is W
    assert all(m.actor is not Actor.REMOTE for m in s.moves)
    assert rig.snapshots[-1].rev < s.rev
