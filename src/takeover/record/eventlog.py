"""イベントログ（JSONL。I10）。

担当: M0（テスト: tests/test_eventlog.py）
参照: SPEC §6.8

- 1 行 1 JSON。項目: t（単調時刻 ms）、wall（壁時計 ISO 8601）、kind、あとは種類ごとの項目。
- kind の例: usi_send / usi_recv / csa_send / csa_recv / mode_change / search_start /
  bestmove_discarded / commit / command_rejected / timer / engine_restart / notice
- 書き込み前に秘密情報を伏せる（mask_secrets）。1 行ごとに flush。
- 書き込みは同期の小さな write で行い、スレッドを使わない（I3）。
- ソークテストでは「違反した局のログだけ保存」できるよう、メモリ上にためて後から書き出す
  モードも用意する。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def mask_secrets(record: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    """record 内の文字列に含まれる秘密（trip / password / UI トークン）を伏せ字にする（純関数）。"""
    raise NotImplementedError("M0")


class EventLog:
    def __init__(
        self,
        path: Path | None,  # None はメモリのみ
        *,
        secrets: tuple[str, ...] = (),
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._secrets = secrets
        self._now = now

    def write(self, kind: str, **fields: Any) -> None:
        raise NotImplementedError("M0")

    def dump(self, path: Path) -> None:
        """メモリにためた分を書き出す。"""
        raise NotImplementedError("M0")

    def close(self) -> None:
        raise NotImplementedError("M0")
