"""GameCore: 盤面・手番・時計・操作モードの唯一の持ち主（I1）。

担当: M2（ローカル）、M5（CSA モードでの切替・TimeGuard）、M8（ヒント・予約切替）
テスト: tests/test_game_core_*.py（スクリプト式エンジン fakes/scripted_engine.py を使う）
参照: SPEC §6.1、§6.2、§7（遷移表とレース規則。変更はユーザー確認が必要）、§12 の Q1〜Q9

骨格：
    run() が self.queue から 1 つ取り出し → 種類ごとのハンドラで状態を更新 →
    _reconcile() でエンジンへの指示を出し直し → スナップショットを配信、を繰り返す。

実装で必ず守ること：
- 状態（cshogi.Board、履歴、モード、時計、フェーズ、expected_search_id、hint_search_id）は
  このクラスだけが持つ。外へ出すのは Snapshot（読み取り専用）だけ。
- bestmove は search_id == expected_search_id のときだけ採用（I5）。奪取で expected_search_id を
  クリアする。hint_search_id の bestmove は表示専用で、コミット経路に入れない（I11）。
- コミットは 1 手番 1 手（I7）。コミット後は相手の手が来るまで、その席の着手を拒否する。
- UI からの指し手は必ず合法手チェック（I8）。コマンドの ply が現在と違えば拒否。
- CSA モードでは、自分の手番でないときに transport.send_move を呼ばない。
- 宣言（人間・エンジンとも）は core.nyugyoku で条件成立を確認してからだけ送る。
- ハンドラ内の await は engine.cancel() など最小限。engine の再起動は待たない
  （EngineReadyEvent で _reconcile し直す）。
- タイマー（TimeGuard、ローカルの時間切れ）は loop.call_later で TimerFired をキューへ入れる。
  手番が変わるたびに turn_token を進め、古い発火は無視する。
- 時刻は注入された now() だけを使う（time.monotonic を直接呼ばない）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..transport.base import Transport
from .events import Event
from .seat import Seat
from .snapshot import Snapshot
from .types import Color, NyugyokuRule

SnapshotSink = Callable[[Snapshot], None]
NoticeSink = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GamePolicy:
    """GameCore の振る舞いを決める設定値（config から app.py が組み立てる）。"""

    margin_ms: int
    time_guard_min_remaining_ms: int  # 0 で無効
    time_guard_action: str  # "auto" / "warn"
    on_engine_resign: str  # "resign" / "ask"（ask は v1）
    on_engine_failure: str  # "restart" / "manual"
    nyugyoku_rule: NyugyokuRule
    hint_enabled: bool = False
    hint_multipv: int = 1


class GameCore:
    def __init__(
        self,
        *,
        seats: dict[Color, Seat],
        transport: Transport,
        policy: GamePolicy,
        on_snapshot: SnapshotSink,
        on_notice: NoticeSink,
        log=None,  # record.eventlog.EventLog
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self._seats = seats
        self._transport = transport
        self._policy = policy
        self._on_snapshot = on_snapshot
        self._on_notice = on_notice
        self._log = log
        self._now = now

    def post(self, event: Event) -> None:
        """イベント・コマンドの唯一の入口。UiGateway / Engine / Transport / タイマーが呼ぶ。"""
        self.queue.put_nowait(event)

    async def run(self) -> None:
        """終局（ENDED）して後片付けが済むまでキューを処理し続ける。"""
        raise NotImplementedError("M2")

    def snapshot(self) -> Snapshot:
        """現在の状態のスナップショット（新しく接続した UI への初回配信用）。"""
        raise NotImplementedError("M2")

    async def _reconcile(self) -> None:
        """各 LocalSeat のエンジンが「今しているべきこと」と現状の差分を埋める（SPEC §6.1）。

        §7.1 の遷移表はこの関数の入出力表として実装・テストする。
        """
        raise NotImplementedError("M2")
