"""スクリプト式エンジン: プロセスを使わない engine.base.Engine の偽実装。

参照: SPEC §9「スクリプト式エンジン」、§7（遷移表とレース規則）

目的は、GameCore のキューに入るイベントの順序をテストコードが完全に決められるようにすること。
子プロセスも時間待ちも使わない。

- think / ponder / ponderhit / analyze / cancel / set_options などの呼び出しを calls に記録する。
- 本物と同じ状態機械を持ち、IDLE 以外で探索を始めたら EngineBusyError（I6 の検出）。
- テスト側の合図で初めてイベントを emit する:
    release_bestmove(move, ponder=None)  … 現在の探索の BestMoveEvent を emit して IDLE へ
    emit_stale_bestmove(search_id, move) … 古い search_id の BestMoveEvent をわざと emit（I5 の検証）
    emit_info(...) / die(reason) / become_ready()
- cancel() は即座に IDLE に戻り、その探索の bestmove は emit しない
  （「cancel 済みなのにキューに残っていた bestmove」は emit_stale_bestmove で再現する）。
- auto_reply を渡すと、think() のたびにそれを呼んで即座に bestmove を emit する
  （対局を最後まで進めるだけのテスト用）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.events import BestMoveEvent, EngineDeadEvent, EngineInfoEvent, EngineReadyEvent
from ..core.types import Color, Score
from ..engine.base import EmitFn, EngineBusyError, EngineError, EngineState, GoTime

_SEARCHING = (EngineState.THINKING, EngineState.PONDERING, EngineState.ANALYZING)


@dataclass(slots=True)
class Call:
    method: str
    args: dict[str, Any] = field(default_factory=dict)


class ScriptedEngine:
    def __init__(
        self,
        *,
        color: Color,
        emit: EmitFn,
        name: str = "scripted",
        auto_reply: Callable[[str], tuple[str, str | None]] | None = None,
    ) -> None:
        self._color = color
        self._emit = emit
        self._name = name
        self._state = EngineState.DEAD
        self._next_id = 0
        self._auto_reply = auto_reply
        self.current_search_id: int | None = None
        self.calls: list[Call] = []
        self.options: dict[str, Any] = {}

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def name(self) -> str:
        return self._name

    @property
    def color(self) -> Color:
        return self._color

    # ---- 記録の参照 ----

    def calls_of(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    def last_call(self, method: str | None = None) -> Call | None:
        calls = self.calls if method is None else self.calls_of(method)
        return calls[-1] if calls else None

    def searches(self) -> list[Call]:
        return [c for c in self.calls if c.method in ("think", "ponder", "analyze")]

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(Call(method, args))

    # ---- Engine インターフェース ----

    async def start(self) -> None:
        self._record("start")
        self._state = EngineState.IDLE

    async def new_game(self) -> None:
        self._record("new_game")

    async def game_over(self, result: str) -> None:
        self._record("game_over", result=result)

    def _begin(self, method: str, state: EngineState, **args: Any) -> int:
        if self._state is not EngineState.IDLE:
            raise EngineBusyError(f"{method}() called while {self._state.value}")
        self._next_id += 1
        self.current_search_id = self._next_id
        self._state = state
        self._record(method, search_id=self._next_id, **args)
        return self._next_id

    async def think(self, position_cmd: str, go_time: GoTime) -> int:
        search_id = self._begin("think", EngineState.THINKING, position=position_cmd, go_time=go_time)
        if self._auto_reply is not None:
            move, ponder = self._auto_reply(position_cmd)
            self.release_bestmove(move, ponder)
        return search_id

    async def ponder(self, position_cmd: str, go_time: GoTime) -> int:
        return self._begin("ponder", EngineState.PONDERING, position=position_cmd, go_time=go_time)

    async def ponderhit(self) -> None:
        if self._state is not EngineState.PONDERING:
            raise EngineError(f"ponderhit() called while {self._state.value}")
        self._state = EngineState.THINKING
        self._record("ponderhit", search_id=self.current_search_id)

    async def analyze(self, position_cmd: str) -> int:
        return self._begin("analyze", EngineState.ANALYZING, position=position_cmd)

    async def cancel(self) -> None:
        self._record("cancel", search_id=self.current_search_id, state=self._state.value)
        if self._state in _SEARCHING:
            self._state = EngineState.IDLE
            self.current_search_id = None

    async def set_options(self, options: Mapping[str, str | int | bool]) -> None:
        if self._state is not EngineState.IDLE:
            raise EngineBusyError(f"set_options() called while {self._state.value}")
        self.options.update(options)
        self._record("set_options", options=dict(options))

    async def quit(self) -> None:
        self._record("quit")
        self._state = EngineState.DEAD
        self.current_search_id = None

    # ---- テストから使う操作 ----

    def release_bestmove(
        self,
        move: str,
        ponder: str | None = None,
        *,
        score: Score | None = None,
        pv: tuple[str, ...] = (),
    ) -> int:
        """現在の探索（THINKING）の bestmove を emit して IDLE に戻る。search_id を返す。"""
        if self._state is not EngineState.THINKING or self.current_search_id is None:
            raise AssertionError(f"release_bestmove() while {self._state.value}")
        search_id = self.current_search_id
        self._state = EngineState.IDLE
        self.current_search_id = None
        self._emit(BestMoveEvent(color=self._color, search_id=search_id, move=move, ponder=ponder,
                                 score=score, pv=pv))
        return search_id

    def emit_stale_bestmove(self, search_id: int, move: str, ponder: str | None = None) -> None:
        """状態を変えずに、指定した search_id の bestmove だけを emit する。"""
        self._emit(BestMoveEvent(color=self._color, search_id=search_id, move=move, ponder=ponder))

    def emit_info(self, **fields: Any) -> None:
        search_id = fields.pop("search_id", self.current_search_id or 0)
        self._emit(EngineInfoEvent(color=self._color, search_id=search_id, **fields))

    def die(self, reason: str = "scripted", *, will_restart: bool = False) -> None:
        self._state = EngineState.STARTING if will_restart else EngineState.DEAD
        self.current_search_id = None
        self._emit(EngineDeadEvent(color=self._color, reason=reason, will_restart=will_restart))

    def become_ready(self) -> None:
        self._state = EngineState.IDLE
        self._emit(EngineReadyEvent(color=self._color, restarted=True))
