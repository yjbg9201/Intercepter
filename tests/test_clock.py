from takeover.core import clock as k
from takeover.core.clock import Clock
from takeover.core.types import Color, TimeControl

FISCHER = TimeControl(total_ms=300_000, increment_ms=10_000)
BYOYOMI = TimeControl(total_ms=60_000, byoyomi_ms=10_000)
B, W = Color.BLACK, Color.WHITE


def test_consumed_floors_to_unit():
    assert k.consumed_ms(999, FISCHER) == 0
    assert k.consumed_ms(12_999, FISCHER) == 12_000
    assert k.consumed_ms(500, TimeControl(0, least_time_per_move_ms=1000)) == 1000


def test_time_up_rule_matches_shogi_server():
    # 残り - t + byoyomi + increment <= 0 で負け（ちょうど 0 も負け）
    assert not k.is_time_up(0, 9_000, BYOYOMI)
    assert k.is_time_up(0, 10_000, BYOYOMI)
    assert not k.is_time_up(5_000, 14_000, FISCHER)
    assert k.is_time_up(5_000, 15_000, FISCHER)
    assert not k.is_time_up(0, 10**9, TimeControl(0))  # 制限なし


def test_flag_after():
    assert k.flag_after_ms(0, BYOYOMI) == 10_000
    assert k.flag_after_ms(5_500, FISCHER) == 16_000  # 秒に切り上げ
    assert k.flag_after_ms(0, TimeControl(0)) is None


def test_increment_added_before_subtract(fake_clock):
    clock = Clock(FISCHER, now=fake_clock)
    clock.set_remaining(B, 3_000)
    clock.apply_move(B, 12_000)  # 3 + 10 - 12 = 1
    assert clock.remaining_ms(B) == 1_000
    clock.apply_move(B, 99_000)
    assert clock.remaining_ms(B) == 0


def test_go_time_subtracts_elapsed_and_margin_for_mover_only(fake_clock):
    clock = Clock(FISCHER, now=fake_clock)
    clock.start_turn(B)
    fake_clock.advance(7_500)
    go = clock.go_time(B, margin_ms=1_000)
    assert (go.btime_ms, go.wtime_ms, go.binc_ms, go.winc_ms) == (291_500, 300_000, 10_000, 10_000)
    assert go.byoyomi_ms is None
    # ponder 用は既経過 0
    assert clock.go_time(W, margin_ms=1_000, pondering=True).wtime_ms == 299_000


def test_go_time_overflow_eats_byoyomi(fake_clock):
    clock = Clock(BYOYOMI, now=fake_clock)
    clock.set_remaining(B, 2_000)
    clock.start_turn(B)
    fake_clock.advance(8_000)  # 持ち時間 2 秒を使い切り、秒読みに 6 秒食い込んだ
    go = clock.go_time(B, margin_ms=1_000)
    assert (go.btime_ms, go.byoyomi_ms) == (0, 3_000)
    fake_clock.advance(60_000)
    assert clock.go_time(B, margin_ms=1_000).byoyomi_ms == 0


def test_manual_budget(fake_clock):
    clock = Clock(FISCHER, now=fake_clock)
    clock.set_remaining(B, 20_000)
    clock.start_turn(B)
    fake_clock.advance(18_000)
    assert clock.manual_budget_ms(B) == 12_000
    assert clock.turn_elapsed_ms() == 18_000 and clock.consumed_now() == 18_000
