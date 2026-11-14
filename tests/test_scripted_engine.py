import pytest

from takeover.core.events import BestMoveEvent, EngineDeadEvent, EngineReadyEvent
from takeover.core.types import Color
from takeover.engine.base import Engine, EngineBusyError, EngineState, GoTime
from takeover.fakes.scripted_engine import ScriptedEngine

GO = GoTime(1000, 1000)


async def make():
    events = []
    engine = ScriptedEngine(color=Color.BLACK, emit=events.append)
    await engine.start()
    return engine, events


async def test_implements_protocol_and_records_calls():
    engine, events = await make()
    assert isinstance(engine, Engine)
    sid = await engine.think("position startpos", GO)
    assert engine.state is EngineState.THINKING and events == []
    assert engine.release_bestmove("7g7f", "3c3d") == sid
    assert events == [BestMoveEvent(color=Color.BLACK, search_id=sid, move="7g7f", ponder="3c3d")]
    assert engine.state is EngineState.IDLE
    assert engine.last_call("think").args["position"] == "position startpos"


async def test_single_search_invariant():
    engine, _ = await make()
    await engine.think("position startpos", GO)
    with pytest.raises(EngineBusyError):
        await engine.think("position startpos", GO)
    with pytest.raises(EngineBusyError):
        await engine.ponder("position startpos", GO)


async def test_cancel_discards_and_ids_increase():
    engine, events = await make()
    first = await engine.think("position startpos", GO)
    await engine.cancel()
    assert engine.state is EngineState.IDLE and events == []
    second = await engine.ponder("position startpos moves 7g7f 3c3d", GO)
    assert second > first
    await engine.ponderhit()
    assert engine.state is EngineState.THINKING
    engine.emit_stale_bestmove(first, "2g2f")
    assert events[-1].search_id == first and engine.state is EngineState.THINKING


async def test_die_and_ready():
    engine, events = await make()
    await engine.think("position startpos", GO)
    engine.die("boom", will_restart=True)
    assert engine.state is EngineState.STARTING and isinstance(events[-1], EngineDeadEvent)
    engine.become_ready()
    assert engine.state is EngineState.IDLE and isinstance(events[-1], EngineReadyEvent)
