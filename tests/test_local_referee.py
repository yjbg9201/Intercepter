"""LocalReferee（SPEC §6.6、§9 シナリオ 20）。"""

from takeover.core import events as ev
from takeover.core.types import Color, EndReason, GameResult, NyugyokuRule, TimeControl
from takeover.transport.local import LocalReferee

B, W = Color.BLACK, Color.WHITE


async def make(fake_clock, **kwargs):
    events = []
    settings = dict(black_name="b", white_name="w", time_control=TimeControl(0), max_moves=256,
                    nyugyoku_rule=NyugyokuRule.LAW_27, emit=events.append, now=fake_clock,
                    call_later=lambda *a: None)
    settings.update(kwargs)
    referee = LocalReferee(**settings)
    await referee.start()
    await referee.agree()
    return referee, events


async def play(referee, moves, first=B):
    color = first
    for usi in moves:
        await referee.send_move(color, usi)
        color = color.opponent


def ended(events):
    return [e for e in events if isinstance(e, ev.GameEnded)]


async def test_offer_start_and_confirm(fake_clock):
    referee, events = await make(fake_clock, time_control=TimeControl(60_000, increment_ms=5_000))
    assert isinstance(events[0], ev.GameOffered) and events[0].my_color is None
    assert isinstance(events[1], ev.GameStarted)
    fake_clock.advance(3_400)
    await referee.send_move(B, "7g7f")
    assert events[-1] == ev.MoveConfirmed(color=B, usi="7g7f", elapsed_ms=3_000)  # 秒未満切り捨て


async def test_sennichite_needs_four_occurrences(fake_clock):
    referee, events = await make(fake_clock)
    cycle = ["5i5h", "5a5b", "5h5i", "5b5a"]
    await play(referee, cycle * 2)
    assert ended(events) == []  # 3 回目まででは成立しない
    await play(referee, cycle)
    (end,) = ended(events)
    assert (end.result, end.reason) == (GameResult.DRAW, EndReason.SENNICHITE)
    await referee.send_move(B, "7g7f")  # 終局後の指し手は無視
    assert len(ended(events)) == 1


async def test_perpetual_check_loses(fake_clock):
    referee, events = await make(fake_clock, start_sfen="5k3/9/9/9/9/9/9/9/K3R4 b - 1")
    await play(referee, ["5i4i", "4a5a", "4i5i", "5a4a"] * 3)
    (end,) = ended(events)
    assert (end.result, end.reason) == (GameResult.WHITE_WIN, EndReason.OUTE_SENNICHITE)


async def test_mate_max_moves_and_illegal(fake_clock):
    # 頭金: 5b に金を打って詰み
    referee, events = await make(fake_clock, start_sfen="4k4/9/4P4/9/9/9/9/9/4K4 b G 1")
    await referee.send_move(B, "G*5b")
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.BLACK_WIN, EndReason.MATE)

    referee, events = await make(fake_clock, max_moves=2)
    await play(referee, ["7g7f", "3c3d"])
    assert ended(events)[0].reason is EndReason.MAX_MOVES and ended(events)[0].result is GameResult.DRAW

    referee, events = await make(fake_clock)
    await referee.send_move(W, "3c3d")  # 手番外は無視（終局にもならない）
    assert ended(events) == []
    await referee.send_move(B, "7g7e")
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.WHITE_WIN, EndReason.ILLEGAL_MOVE)


async def test_declaration_and_resign(fake_clock):
    referee, events = await make(fake_clock, start_sfen="RRBBK4/GGGGSS3/9/9/9/9/9/9/4k4 b 2P 1")
    await referee.send_declare(B)
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.BLACK_WIN, EndReason.JISHOGI)
    referee, events = await make(fake_clock)
    await referee.send_declare(B)  # 条件不成立の宣言は負け
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.WHITE_WIN, EndReason.ILLEGAL_DECLARATION)
    referee, events = await make(fake_clock)
    await referee.send_resign(B)
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.WHITE_WIN, EndReason.RESIGN)


async def test_move_after_flag_falls_is_time_up(fake_clock):
    referee, events = await make(fake_clock, time_control=TimeControl(5_000, byoyomi_ms=3_000))
    fake_clock.advance(8_000)
    await referee.send_move(B, "7g7f")
    assert (ended(events)[0].result, ended(events)[0].reason) == (GameResult.WHITE_WIN, EndReason.TIME_UP)
    assert not any(isinstance(e, ev.MoveConfirmed) for e in events)
