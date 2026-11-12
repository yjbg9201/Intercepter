"""UiGateway: HTTP と WebSocket の入口（SPEC §6.7）。

担当: M4（PC。テスト: tests/test_ui_gateway.py。§9 シナリオ 19）、M6（LAN 公開・トークン。シナリオ 17）

実装で必ず守ること：
- 状態を持たない。受け取ったメッセージを ui.messages で Command にして GameCore.post() へ渡し、
  GameCore から来た Snapshot / notice を全接続へ配るだけ（I1, I8）。合法手判定をしない。
- 新しい接続には、まず最新のスナップショットを 1 回送る（再読み込みで完全に復元できること）。
- engine_info は最大 5 回/秒に間引く。
- WebSocket のハンドシェイクで Origin を検査する。HTTP は Host を検査する（v0 から）。
- 状態を変える HTTP エンドポイントを作らない（変更は WebSocket のコマンドだけ）。
- 既定のバインドは 127.0.0.1。0.0.0.0 にするのは ui.lan.enabled のときだけで、そのときは
  トークン必須（トークンなしで 0.0.0.0 にバインドするコードパスを作らない）。
- トークンをログ・スナップショット・エラーメッセージに出さない。比較は hmac.compare_digest。
- uvicorn は同じイベントループ上で動かす（スレッドを使わない。I3）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..core.events import Command, EngineInfoEvent
from ..core.snapshot import Snapshot


@dataclass(frozen=True, slots=True)
class UiSettings:
    host: str
    port: int
    lan_enabled: bool
    token: str  # 秘密。lan_enabled のとき必須（空なら app.py が起動時に生成して渡す）
    allowed_hosts: tuple[str, ...]  # Origin / Host の許可リスト（localhost 系は常に許可）
    notify_my_turn: bool


class UiGateway:
    def __init__(
        self,
        settings: UiSettings,
        *,
        post_command: Callable[[Command], None],
        get_snapshot: Callable[[], Snapshot],
    ) -> None:
        self._settings = settings
        self._post_command = post_command
        self._get_snapshot = get_snapshot

    async def serve(self) -> None:
        """uvicorn を現在のイベントループで起動し、停止されるまで動かす。"""
        raise NotImplementedError("M4")

    def publish_snapshot(self, snapshot: Snapshot) -> None:
        """全接続へ配る。GameCore の on_snapshot に渡す。ブロックしないこと。"""
        raise NotImplementedError("M4")

    def publish_notice(self, text: str) -> None:
        raise NotImplementedError("M4")

    def publish_engine_info(self, info: EngineInfoEvent) -> None:
        raise NotImplementedError("M4")

    async def shutdown(self) -> None:
        raise NotImplementedError("M4")
