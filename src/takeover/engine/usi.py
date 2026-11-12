"""EngineController: USI エンジンの子プロセスの唯一の所有者（I2）。engine.base.Engine の実装。

担当: M1（テスト: tests/test_engine_controller.py、偽エンジン fakes/fake_engine.py を使う）
参照: SPEC §6.3（状態機械・規則）、§7.4（異常時）、CLAUDE.md「過去実装で踏んだ不具合」

実装で必ず守ること：
- stdout を読むのは専用タスク 1 つだけ。各行の `\\r` を除去してから usi_parse に渡す。
- stderr は DEVNULL（I9）。StreamReader の limit は設定値（usi_line_limit_bytes）。
- 探索ごとに search_id を振る。STOPPING 中に来た bestmove は破棄する（I5）。
- IDLE 以外で think / ponder / analyze が呼ばれたら、送信せず EngineBusyError（I6）。
- PONDERING 中に stop / ponderhit なしで来た bestmove は破棄して IDLE に戻る。
- cancel(): stop → stop_grace_ms 待つ → 来なければ kill して return。再起動は裏のタスクで行い、
  EngineReadyEvent / EngineDeadEvent を emit する。再起動は 1 局 max_restarts 回まで。
- 起動の各待ち（usiok / readyok）にタイムアウトとプロセス死亡検知。
- stdin への書き込み失敗（BrokenPipe）はプロセス死亡として扱い、例外を外へ漏らさない。
- quit(): quit → 待機 → stdin クローズ → 待機 → kill → wait。
- 送受信した生の行と、search_id の払い出し・破棄した bestmove をイベントログへ（I10）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..core.types import Color
from .base import EmitFn, EngineState, GoTime
from .launch import LaunchSpec


@dataclass(frozen=True, slots=True)
class EngineTimeouts:
    """すべて設定値から渡す（ハードコードしない）。"""

    usi_timeout_ms: int
    stop_grace_ms: int
    quit_timeout_ms: int
    max_restarts: int
    line_limit_bytes: int


class EngineController:
    def __init__(
        self,
        *,
        color: Color,
        launch: LaunchSpec,
        options: Mapping[str, str | int | bool],
        ponder_enabled: bool,
        timeouts: EngineTimeouts,
        emit: EmitFn,
        log=None,  # record.eventlog.EventLog
    ) -> None:
        self._color = color
        self._launch = launch
        self._options = dict(options)
        self._ponder_enabled = ponder_enabled
        self._timeouts = timeouts
        self._emit = emit
        self._log = log
        self._state = EngineState.DEAD
        self._name = launch.display_name

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        raise NotImplementedError("M1")

    async def new_game(self) -> None:
        raise NotImplementedError("M1")

    async def game_over(self, result: str) -> None:
        raise NotImplementedError("M1")

    async def think(self, position_cmd: str, go_time: GoTime) -> int:
        raise NotImplementedError("M1")

    async def ponder(self, position_cmd: str, go_time: GoTime) -> int:
        raise NotImplementedError("M1")

    async def ponderhit(self) -> None:
        raise NotImplementedError("M1")

    async def analyze(self, position_cmd: str) -> int:
        raise NotImplementedError("M8")

    async def cancel(self) -> None:
        raise NotImplementedError("M1")

    async def set_options(self, options: Mapping[str, str | int | bool]) -> None:
        raise NotImplementedError("M1")

    async def quit(self) -> None:
        raise NotImplementedError("M1")
