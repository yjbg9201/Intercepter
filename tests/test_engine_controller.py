"""EngineController（SPEC §6.3、§9 シナリオ 1, 2, 5〜8, 21, 23、CLAUDE.md の過去不具合表）。

順序は偽エンジンの引数（思考時間・stop 応答遅延など）で決める。偶然の sleep には頼らない。
"""

import asyncio
import os

import cshogi
import pytest

from conftest import fake_engine_argv
from takeover.core.events import BestMoveEvent, EngineDeadEvent, EngineInfoEvent, EngineReadyEvent
from takeover.core.types import Color
from takeover.engine.base import Engine, EngineBusyError, EngineError, EngineStartError, EngineState, GoTime
from takeover.engine.launch import EngineKind, LaunchSpec
from takeover.engine.usi import EngineController, EngineTimeouts
from takeover.record.eventlog import EventLog

GO = GoTime(60_000, 60_000, byoyomi_ms=1000)
START = "position startpos"
LONG = ("--think-ms", "600000")  # stop するまで返さない


class Harness:
    def __init__(self, *args, ponder=False, argv=None, **timeouts):
        self.events: asyncio.Queue = asyncio.Queue()
        self.log = EventLog(None)
        settings = dict(usi_timeout_ms=5000, stop_grace_ms=3000, quit_timeout_ms=2000,
                        max_restarts=3, line_limit_bytes=1 << 20, info_interval_ms=0)
        settings.update(timeouts)
        spec = LaunchSpec(argv=argv or fake_engine_argv(*args), cwd=None, kind=EngineKind.LINUX,
                          display_name="fake")
        self.engine = EngineController(
            color=Color.BLACK, launch=spec, options={"Threads": 1}, ponder_enabled=ponder,
            timeouts=EngineTimeouts(**settings), emit=self.events.put_nowait, log=self.log)

    async def next(self, kind, timeout=10):
        """kind のイベントが来るまで待つ（info などは読み飛ばす）。"""
        while True:
            event = await asyncio.wait_for(self.events.get(), timeout)
            if isinstance(event, kind):
                return event

    def pending(self, kind):
        items = []
        while not self.events.empty():
            items.append(self.events.get_nowait())
        return [e for e in items if isinstance(e, kind)]

    def sent(self):
        return [r["line"] for r in self.log.records if r["kind"] == "usi_send"]

    def kinds(self, kind):
        return [r for r in self.log.records if r["kind"] == kind]


@pytest.fixture
async def make():
    made = []

    async def factory(*args, start=True, **kwargs):
        h = Harness(*args, **kwargs)
        made.append(h)
        if start:
            await h.engine.start()
        return h

    yield factory
    for h in made:
        await h.engine.quit()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---- 起動・終了 ----------------------------------------------------------- #


async def test_start_handshake_and_options(make):
    h = await make(ponder=True)
    assert isinstance(h.engine, Engine)
    assert h.engine.state is EngineState.IDLE and h.engine.name == "FakeEngine"
    sent = h.sent()
    assert sent[0] == "usi" and sent[-1] == "isready"
    assert "setoption name USI_Ponder value true" in sent and "setoption name Threads value 1" in sent


async def test_quit_waits_for_exit_and_is_idempotent(make):
    h = await make()
    pid = h.engine.pid
    await h.engine.quit()
    assert h.engine.state is EngineState.DEAD and not alive(pid)
    await h.engine.quit()


async def test_quit_kills_engine_that_ignores_quit_and_eof(make):
    h = await make("--ignore-quit", "--no-exit-on-eof", quit_timeout_ms=200)
    pid = h.engine.pid
    await h.engine.quit()
    assert not alive(pid) and h.engine.state is EngineState.DEAD


@pytest.mark.parametrize("flag, token", [("--no-usiok", "usiok"), ("--no-readyok", "readyok")])
async def test_startup_timeout_does_not_wait_forever(make, flag, token):
    h = await make(flag, start=False, usi_timeout_ms=300)
    with pytest.raises(EngineStartError, match=token):
        await h.engine.start()
    assert h.engine.state is EngineState.DEAD and h.engine.pid is None


async def test_startup_detects_process_death(make):
    h = await make(start=False, argv=("/bin/sh", "-c", "exit 7"), usi_timeout_ms=5000)
    with pytest.raises(EngineStartError, match="終了"):
        await asyncio.wait_for(h.engine.start(), 3)  # タイムアウトを待たずに検知する


async def test_missing_binary(make):
    h = await make(start=False, argv=("/nonexistent/engine",))
    with pytest.raises(EngineStartError):
        await h.engine.start()


# ---- 探索 ----------------------------------------------------------------- #


async def test_think_returns_legal_bestmove_with_ponder_pair(make):
    h = await make("--ponder", ponder=True)
    await h.engine.new_game()
    sid = await h.engine.think(START, GO)
    assert h.engine.state is EngineState.THINKING
    best = await h.next(BestMoveEvent)
    assert best.search_id == sid and best.color is Color.BLACK
    board = cshogi.Board()
    assert board.is_legal(board.move_from_usi(best.move))
    board.push_usi(best.move)
    assert best.ponder and board.is_legal(board.move_from_usi(best.ponder))  # (move, ponder) の組
    assert best.score is not None and best.pv[0] == best.move
    assert h.engine.state is EngineState.IDLE
    sent = h.sent()
    assert sent[-3:] == ["usinewgame", START, "go btime 60000 wtime 60000 byoyomi 1000"]


async def test_crlf_engine(make):
    h = await make("--crlf")
    await h.engine.think(START, GO)
    best = await h.next(BestMoveEvent)
    assert "\r" not in best.move
    assert all("\r" not in r["line"] for r in h.kinds("usi_recv"))


async def test_info_events_carry_search_id(make):
    h = await make("--multipv", "2")
    sid = await h.engine.think(START, GO)
    infos = []
    while True:
        event = await asyncio.wait_for(h.events.get(), 10)
        if isinstance(event, BestMoveEvent):
            break
        infos.append(event)
    assert {i.multipv for i in infos if isinstance(i, EngineInfoEvent)} == {1, 2}
    assert all(i.search_id == sid for i in infos)


async def test_only_one_search_at_a_time(make):
    h = await make(*LONG)
    await h.engine.think(START, GO)
    before = len(h.sent())
    for call in (h.engine.think(START, GO), h.engine.ponder(START, GO), h.engine.analyze(START)):
        with pytest.raises(EngineBusyError):
            await call
    with pytest.raises(EngineBusyError):
        await h.engine.set_options({"MultiPV": 3})
    assert len(h.sent()) == before  # 何も送っていない（I6）


async def test_cancel_discards_bestmove_and_next_search_gets_new_id(make):
    h = await make(*LONG)
    first = await h.engine.think(START, GO)
    await h.engine.cancel()
    assert h.engine.state is EngineState.IDLE
    assert h.pending(BestMoveEvent) == []  # stop 後の bestmove はイベントにならない
    discarded = h.kinds("bestmove_discarded")
    assert len(discarded) == 1 and discarded[0]["search_id"] == first
    await h.engine.cancel()  # IDLE なら即 return
    assert h.sent().count("stop") == 1


async def test_cancel_waits_for_slow_stop_response_instead_of_restarting(make):
    """過去不具合: stop 直後に待機を抜けていた。stop_grace_ms までは bestmove を待つ。"""
    h = await make(*LONG, "--stop-delay-ms", "400", stop_grace_ms=5000)
    pid = h.engine.pid
    await h.engine.think(START, GO)
    await h.engine.cancel()
    assert h.engine.state is EngineState.IDLE and h.engine.pid == pid
    assert h.pending(EngineDeadEvent) == []
    h2 = await make("--think-ms", "0")
    second = await h2.engine.think(START, GO)
    assert (await h2.next(BestMoveEvent)).search_id == second


async def test_search_after_cancel_uses_fresh_id(make):
    h = await make("--stop-delay-ms", "0", "--think-ms", "600000")
    first = await h.engine.think(START, GO)
    await h.engine.cancel()
    # 以後は即答するエンジンとして振る舞わせられないので、ponder → ponderhit は別テストで見る
    second = await h.engine.analyze(START)
    assert second > first and h.sent()[-1] == "go infinite"
    await h.engine.cancel()
    assert h.pending(BestMoveEvent) == []


async def test_ponder_hit(make):
    h = await make("--think-ms", "0")
    sid = await h.engine.ponder("position startpos moves 7g7f 3c3d", GO)
    assert h.engine.state is EngineState.PONDERING
    assert h.sent()[-1].startswith("go ponder btime")
    await h.engine.ponderhit()
    best = await h.next(BestMoveEvent)
    assert best.search_id == sid and h.sent()[-1] == "ponderhit"


async def test_ponder_miss(make):
    h = await make("--think-ms", "0")
    first = await h.engine.ponder("position startpos moves 7g7f 3c3d", GO)
    await h.engine.cancel()
    assert h.pending(BestMoveEvent) == []
    second = await h.engine.think("position startpos moves 7g7f 8c8d", GO)
    best = await h.next(BestMoveEvent)
    assert best.search_id == second != first
    with pytest.raises(EngineError):
        await h.engine.ponderhit()  # ponder 中でない


async def test_rogue_bestmove_during_ponder_is_discarded(make):
    """シナリオ 21: ponder 中に stop / ponderhit なしで bestmove が来る（USI 違反）。"""
    h = await make("--rogue-ponder-bestmove", "--think-ms", "0")
    await h.engine.ponder("position startpos moves 7g7f 3c3d", GO)
    await h.engine.wait_not_searching(5000)
    assert h.engine.state is EngineState.IDLE and h.pending(BestMoveEvent) == []
    assert len(h.kinds("bestmove_discarded")) == 1
    sid = await h.engine.think("position startpos moves 7g7f 3c3d", GO)  # 通常の go ができる
    assert (await h.next(BestMoveEvent)).search_id == sid


async def test_gameover_during_ponder(make):
    """シナリオ 5: ponder 中の終局は cancel → gameover。"""
    h = await make(*LONG)
    await h.engine.new_game()
    await h.engine.ponder("position startpos moves 7g7f 3c3d", GO)
    await h.engine.cancel()
    await h.engine.game_over("lose")
    assert h.sent()[-2:] == ["stop", "gameover lose"]
    assert h.pending(BestMoveEvent) == []


async def test_long_info_line_does_not_stop_reader(make):
    """シナリオ 23。"""
    h = await make("--huge-info", "200000", line_limit_bytes=4096)
    sid = await h.engine.think(START, GO)
    assert (await h.next(BestMoveEvent)).search_id == sid
    assert h.kinds("usi_line_too_long")


# ---- 異常 ----------------------------------------------------------------- #


async def test_stop_ignored_kills_and_restarts_in_background(make):
    """シナリオ 6: cancel() は再起動を待たずに戻り、再起動後は探索できる。"""
    h = await make("--ignore-stop", "--think-ms", "0", stop_grace_ms=200)
    await h.engine.new_game()
    old_pid = h.engine.pid
    # think-ms 0 だと即答してしまうので、ponder（stop / ponderhit 待ち）で止まらせる
    await h.engine.ponder("position startpos moves 7g7f 3c3d", GO)
    await h.engine.cancel()
    assert h.engine.state is EngineState.STARTING  # 再起動を待たずに戻っている
    assert not alive(old_pid)
    dead = await h.next(EngineDeadEvent)
    assert dead.will_restart
    ready = await h.next(EngineReadyEvent)
    assert ready.restarted and h.engine.state is EngineState.IDLE and h.engine.pid != old_pid
    assert h.sent()[-1] == "usinewgame"  # 再起動後に送り直す
    sid = await h.engine.think(START, GO)
    assert (await h.next(BestMoveEvent)).search_id == sid


async def test_crash_restarts_then_gives_up_at_limit(make):
    """シナリオ 7 の EngineController 側。再起動は max_restarts 回まで。"""
    h = await make("--crash-on-go", "1", max_restarts=1)
    first = await h.engine.think(START, GO)
    assert (await h.next(EngineDeadEvent)).will_restart
    await h.next(EngineReadyEvent)
    second = await h.engine.think(START, GO)  # 再起動したプロセスも最初の go で落ちる
    assert second > first
    dead = await h.next(EngineDeadEvent)
    assert not dead.will_restart and h.engine.state is EngineState.DEAD
    assert h.pending(BestMoveEvent) == []
    with pytest.raises(EngineBusyError):
        await h.engine.think(START, GO)
    await h.engine.cancel()  # DEAD でも安全に呼べる


async def test_new_game_resets_restart_budget(make):
    h = await make("--crash-on-go", "1", max_restarts=1)
    for _ in range(2):
        await h.engine.new_game()
        await h.engine.think(START, GO)
        assert (await h.next(EngineDeadEvent)).will_restart
        await h.next(EngineReadyEvent)


async def test_no_child_left_after_quit_in_any_state(make):
    pids = []
    for args, action in ((LONG, "think"), (("--ignore-stop",), "ponder"), ((), "idle")):
        h = await make(*args, stop_grace_ms=100, quit_timeout_ms=300)
        pids.append(h.engine.pid)
        if action == "think":
            await h.engine.think(START, GO)
        elif action == "ponder":
            await h.engine.ponder(START, GO)
        await h.engine.quit()
        assert h.engine.state is EngineState.DEAD
    assert not any(alive(p) for p in pids)
