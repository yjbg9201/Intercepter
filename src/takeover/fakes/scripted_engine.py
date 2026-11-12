"""スクリプト式エンジン: プロセスを使わない engine.base.Engine の偽実装。

担当: M0（GameCore のテスト M2 / M5 / M8 の土台）
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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Color
from ..engine.base import EmitFn, EngineState, GoTime


@dataclass(slots=True)
class Call:
    method: str
    args: dict[str, Any] = field(default_factory=dict)


class ScriptedEngine:
    def __init__(self, *, color: Color, emit: EmitFn, name: str = "scripted") -> None:
        self._color = color
        self._emit = emit
        self._name = name
        self._state = EngineState.DEAD
        self.calls: list[Call] = []

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        raise NotImplementedError("M0")

    async def new_game(self) -> None:
        raise NotImplementedError("M0")

    async def game_over(self, result: str) -> None:
        raise NotImplementedError("M0")

    async def think(self, position_cmd: str, go_time: GoTime) -> int:
        raise NotImplementedError("M0")

    async def ponder(self, position_cmd: str, go_time: GoTime) -> int:
        raise NotImplementedError("M0")

    async def ponderhit(self) -> None:
        raise NotImplementedError("M0")

    async def analyze(self, position_cmd: str) -> int:
        raise NotImplementedError("M0")

    async def cancel(self) -> None:
        raise NotImplementedError("M0")

    async def set_options(self, options: Mapping[str, str | int | bool]) -> None:
        raise NotImplementedError("M0")

    async def quit(self) -> None:
        raise NotImplementedError("M0")

    # ---- テストから使う操作 ----

    def release_bestmove(self, move: str, ponder: str | None = None) -> None:
        raise NotImplementedError("M0")

    def emit_stale_bestmove(self, search_id: int, move: str) -> None:
        raise NotImplementedError("M0")

    def die(self, reason: str = "scripted") -> None:
        raise NotImplementedError("M0")

    def become_ready(self) -> None:
        raise NotImplementedError("M0")
