"""エンジンのインターフェース。ここは定義済み（骨組みの契約）。

GameCore はこの Protocol にだけ依存する。実装は 2 つ：
- engine.usi.EngineController     : 本物（子プロセス＋USI）
- fakes.scripted_engine.ScriptedEngine : プロセスなしの偽物（GameCore のテスト用）

共通の約束（SPEC §6.3、I5, I6）：
- 未完了の探索は常に 1 つまで。IDLE 以外で think / ponder / analyze を呼んだら
  何も送らずに EngineBusyError を送出する（呼び出し側のバグとして検出する）。
- 探索を始めるメソッドは search_id（プロセスをまたいで単調増加）を返す。
- 結果は戻り値ではなく、コンストラクタで受け取った emit コールバックで
  core.events の EngineEvent として通知する（GameCore のキューに入る）。
- cancel() した探索の bestmove は実装の中で破棄し、イベントにしない。
- cancel() は再起動を待たない。最長でも stop_grace_ms＋強制終了の時間で return する。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ..core.events import EngineEvent

EmitFn = Callable[[EngineEvent], None]
"""イベントの通知先。GameCore の queue.put_nowait を渡す。同期関数で、ブロックしないこと。"""


class EngineState(str, Enum):
    STARTING = "starting"  # 起動中・再起動中（usi 〜 readyok）
    IDLE = "idle"
    THINKING = "thinking"  # go（結果をコミットしうる探索）
    PONDERING = "pondering"  # go ponder
    ANALYZING = "analyzing"  # go infinite（ヒント探索）
    STOPPING = "stopping"  # stop を送って bestmove 待ち
    DEAD = "dead"  # プロセスなし。再起動しない／できない


class EngineError(Exception):
    pass


class EngineBusyError(EngineError):
    """IDLE でないのに探索を始めようとした（I6 違反。呼び出し側のバグ）。"""


class EngineStartError(EngineError):
    """起動に失敗した（実行ファイルがない、usiok / readyok がタイムアウト、起動中に終了）。"""


@dataclass(frozen=True, slots=True)
class GoTime:
    """go の時間引数（ms）。計算は core.clock が行う（SPEC §6.4）。"""

    btime_ms: int
    wtime_ms: int
    byoyomi_ms: int | None = None  # 秒読みがあるとき
    binc_ms: int | None = None  # フィッシャーのとき
    winc_ms: int | None = None


@runtime_checkable
class Engine(Protocol):
    @property
    def state(self) -> EngineState: ...

    @property
    def name(self) -> str:
        """`id name` の値（起動前は設定上の名前）。"""
        ...

    async def start(self) -> None:
        """起動して IDLE になるまで待つ。失敗したら EngineStartError。"""
        ...

    async def new_game(self) -> None:
        """usinewgame。対局開始時と再起動後に必ず呼ぶ。"""
        ...

    async def game_over(self, result: str) -> None:
        """gameover win|lose|draw。"""
        ...

    async def think(self, position_cmd: str, go_time: GoTime) -> int:
        """position ＋ go。IDLE でのみ可。search_id を返す。"""
        ...

    async def ponder(self, position_cmd: str, go_time: GoTime) -> int:
        """position（予想手まで進めた局面）＋ go ponder。IDLE でのみ可。"""
        ...

    async def ponderhit(self) -> None:
        """PONDERING → THINKING。search_id は ponder() のものを引き継ぐ。"""
        ...

    async def analyze(self, position_cmd: str) -> int:
        """position ＋ go infinite（ヒント探索。v1）。IDLE でのみ可。"""
        ...

    async def cancel(self) -> None:
        """探索中なら stop し、bestmove を破棄して戻る。IDLE / DEAD なら即 return。"""
        ...

    async def set_options(self, options: Mapping[str, str | int | bool]) -> None:
        """setoption。IDLE でのみ可。"""
        ...

    async def quit(self) -> None:
        """quit → 待機 → stdin クローズ → 待機 → kill → wait まで行う。何度呼んでもよい。"""
        ...
