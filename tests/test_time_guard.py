"""TimeGuard（SPEC §7.4、§9 シナリオ 10、Q4, Q5）。時間は偽の時計、タイマーは手で発火させる。"""

from rig import B, W, Rig
from takeover.core import events as ev
from takeover.core.game import GamePolicy
from takeover.core.types import Mode, TimeControl

GUARD = GamePolicy(time_guard_min_remaining_ms=15_000, time_guard_action="auto")
TC = TimeControl(total_ms=20_000)


async def test_time_guard_returns_control_to_engine(fake_clock):
    rig = await Rig(fake_clock, policy=GUARD, tc=TC).start()
    await rig.take(B)
    (timer,) = rig.live_timers("time_guard")
    assert round(timer.delay * 1000) == 5_001  # 20 秒 − 閾値 15 秒
    fake_clock.advance(5_001)
    timer.fire()
    await rig.pump()
    assert rig.seats[B].mode is Mode.AUTO
    assert any("TimeGuard" in n for n in rig.core.notices)
    go = rig.engines[B].last_call("think").args["go_time"]
    assert go.btime_ms == 20_000 - 5_001 - 1_000  # 既経過と margin を引いた時間で go


async def test_retake_is_rejected_while_under_threshold(fake_clock):
    """Q4: 往復しない。"""
    rig = await Rig(fake_clock, policy=GUARD, tc=TC).start()
    fake_clock.advance(6_000)
    await rig.take(B)
    assert rig.seats[B].mode is Mode.AUTO
    assert rig.engines[B].calls_of("cancel") == []  # エンジンの探索は止めていない
    assert any("奪取できません" in n for n in rig.core.notices)


async def test_early_or_stale_timer_is_ignored(fake_clock):
    rig = await Rig(fake_clock, policy=GUARD, tc=TC).start()
    await rig.take(B)
    (timer,) = rig.live_timers("time_guard")
    timer.fire()  # まだ閾値に達していない → 張り直すだけ
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL and len(rig.live_timers("time_guard")) == 1
    await rig.human_move(B, "7g7f")
    assert rig.live_timers("time_guard") == []  # 着手でタイマーは外れる
    fake_clock.advance(60_000)
    timer.fire()  # 古い手番のタイマーが遅れて発火
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL and not any("AUTO に戻しました" in n for n in rig.core.notices)


async def test_manual_turn_starting_under_threshold_returns_immediately(fake_clock):
    rig = await Rig(fake_clock, policy=GUARD, tc=TimeControl(total_ms=10_000)).start()
    await rig.engine_move(B, "7g7f")
    # 相手手番中は budget が閾値未満なので奪取そのものが拒否される
    await rig.take(B)
    assert rig.seats[B].mode is Mode.AUTO


async def test_warn_action_only_notifies(fake_clock):
    policy = GamePolicy(time_guard_min_remaining_ms=15_000, time_guard_action="warn")
    rig = await Rig(fake_clock, policy=policy, tc=TC).start()
    fake_clock.advance(6_000)
    await rig.take(B)  # warn のときは奪取を止めない
    assert rig.seats[B].mode is Mode.MANUAL
    (timer,) = rig.live_timers("time_guard")
    timer.fire()
    await rig.pump()
    assert rig.seats[B].mode is Mode.MANUAL and any("少なくなっています" in n for n in rig.core.notices)


async def test_disabled_time_guard_sets_no_timer(fake_clock):
    rig = await Rig(fake_clock, tc=TC).start()
    await rig.take(B)
    assert rig.live_timers("time_guard") == []


async def test_byoyomi_counts_toward_budget(fake_clock):
    """Q5: 式は「残り − 既経過 ＋ 秒読み（＋加算）」。"""
    rig = await Rig(fake_clock, policy=GUARD, tc=TimeControl(total_ms=10_000, byoyomi_ms=30_000)).start()
    await rig.take(B)
    (timer,) = rig.live_timers("time_guard")
    assert round(timer.delay * 1000) == 25_001
