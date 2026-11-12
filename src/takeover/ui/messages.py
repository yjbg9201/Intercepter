"""WebSocket メッセージ（JSON）⇔ コマンド／スナップショットの変換（純関数。I/O なし）。

担当: M4（テスト: tests/test_ui_messages.py）
参照: SPEC §6.7「WebSocket メッセージ」

- UI→サーバ: move {color, ply, usi} / set_mode {color, mode} / resign {color, ply} /
  declare {color, ply} / set_hint {enabled}
- サーバ→UI: snapshot / engine_info / hint / notice / log
- 壊れた入力・未知の type・型の違う項目は例外にせず None を返す（呼び出し側が notice で知らせる）。
- 評価値を「自分視点」に直すのはここ（Score は探索時の手番側視点で届く）。
- 秘密情報（trip / password / UI トークン）を出力に含めない。
"""

from __future__ import annotations

from typing import Any

from ..core.events import Command, EngineInfoEvent
from ..core.snapshot import Snapshot


def parse_client_message(raw: str, *, client_id: str) -> Command | None:
    """UI から届いた JSON 文字列を Command にする。解釈できなければ None。"""
    raise NotImplementedError("M4")


def snapshot_message(snapshot: Snapshot) -> dict[str, Any]:
    """`{"type": "snapshot", ...}`。"""
    raise NotImplementedError("M4")


def engine_info_message(info: EngineInfoEvent) -> dict[str, Any]:
    raise NotImplementedError("M4")


def notice_message(text: str) -> dict[str, Any]:
    raise NotImplementedError("M4")
