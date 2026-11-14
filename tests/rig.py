"""GameCore のテスト用の装置: スクリプト式エンジン＋LocalReferee＋偽の時計。

イベントの順序はテストが決める（engine.release_bestmove / core.post の呼び出し順 = キューの順）。
"""

from __future__ import annotations

from takeover.core import events as ev
from takeover.core.game import GameCore, GamePolicy
from takeover.core.seat import LocalSeat
from takeover.core.types import Color, Mode, NyugyokuRule, TimeControl
from takeover.fakes.scripted_engine import ScriptedEngine
from takeover.record.eventlog import EventLog
from takeover.transport.local import LocalReferee

B, W = Color.BLACK, Color.WHITE
FISCHER = TimeControl(total_ms=300_000, increment_ms=10_000)


class Timer:
    def __init__(self, delay, callback, args):
        self.delay, self.callback, self.args = delay, callback, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback(*self.args)


class Rig:
    def __init__(self, clock, *, policy=None, tc=FISCHER, ponder=False, start_sfen=None,
                 engines=(True, True), max_moves=256, auto_reply=None, transport=None):
        self.clock = clock
        self.timers: list[Timer] = []
        self.log = EventLog(None, now=clock)
        self.snapshots = []
        self.engines: dict[Color, ScriptedEngine] = {}
        seats = {}
        for color, has_engine in zip((B, W), engines):
            engine = None
            if has_engine:
                engine = ScriptedEngine(color=color, emit=self._emit, name=f"eng_{color.value}",
                                        auto_reply=auto_reply)
                self.engines[color] = engine
            seats[color] = LocalSeat(color=color, name=f"p_{color.value}", engine=engine,
                                     mode=Mode.AUTO if engine else Mode.MANUAL, ponder_enabled=ponder)
        self.seats = seats
        self.referee = transport or LocalReferee(
            black_name="p_b", white_name="p_w", time_control=tc, max_moves=max_moves,
            nyugyoku_rule=NyugyokuRule.LAW_27, emit=self._emit, now=clock,
            call_later=self.call_later, start_sfen=start_sfen)
        self.core = GameCore(
            seats=seats, transport=self.referee, policy=policy or GamePolicy(), log=self.log, now=clock,
            call_later=self.call_later, strict=True, on_snapshot=self.snapshots.append)

    def _emit(self, event):
        self.core.post(event)

    def call_later(self, delay, callback, *args):
        timer = Timer(delay, callback, args)
        self.timers.append(timer)
        return timer

    # ---- 操作 ----

    async def start(self):
        for engine in self.engines.values():
            await engine.start()
        await self.referee.start()
        await self.pump()
        return self

    async def pump(self):
        await self.core.process_pending()

    async def engine_move(self, color, move, ponder=None, **kwargs):
        self.engines[color].release_bestmove(move, ponder, **kwargs)
        await self.pump()

    async def take(self, color):
        self.core.post(ev.SetModeCommand(color=color, mode=Mode.MANUAL, client_id="t"))
        await self.pump()

    async def release(self, color):
        self.core.post(ev.SetModeCommand(color=color, mode=Mode.AUTO, client_id="t"))
        await self.pump()

    async def human_move(self, color, usi, ply=None):
        ply = len(self.core.moves) if ply is None else ply
        self.core.post(ev.MoveCommand(color=color, ply=ply, usi=usi, client_id="t"))
        await self.pump()

    # ---- 参照 ----

    @property
    def snap(self):
        return self.core.snapshot()

    def usi(self):
        return [m.usi for m in self.core.moves]

    def actors(self):
        return [m.actor.value for m in self.core.moves]

    def searches(self, color):
        return [(c.method, c.args.get("position")) for c in self.engines[color].searches()]

    def logged(self, kind):
        return [r for r in self.log.records if r["kind"] == kind]

    def live_timers(self, kind=None):
        timers = [t for t in self.timers if not t.cancelled]
        if kind:
            timers = [t for t in timers if t.args and getattr(t.args[0], "kind", None) == kind]
        return timers
