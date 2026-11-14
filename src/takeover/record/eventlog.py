"""イベントログ（JSONL。I10）。

参照: SPEC §6.8

- 1 行 1 JSON。項目: t（単調時刻 ms）、wall（壁時計 ISO 8601）、kind、あとは種類ごとの項目。
- kind の例: usi_send / usi_recv / csa_send / csa_recv / mode_change / search_start /
  bestmove_discarded / commit / command_rejected / timer / engine_restart / notice
- 書き込み前に秘密情報を伏せる（mask_secrets）。1 行ごとに flush。
- 書き込みは同期の小さな write で行い、スレッドを使わない（I3）。
- path=None はメモリのみ。ソークテストで「違反した局のログだけ保存」するのに dump() を使う。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MASK = "***"


def _mask(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {k: _mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, secrets) for v in value]
    return value


def mask_secrets(record: dict[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    """record 内の文字列に含まれる秘密（trip / password / UI トークン）を伏せ字にする（純関数）。"""
    return _mask(record, tuple(s for s in secrets if s))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and not callable(value.value):  # Enum
        return _jsonable(value.value)
    return repr(value)


class EventLog:
    def __init__(
        self,
        path: Path | None,  # None はメモリのみ
        *,
        secrets: tuple[str, ...] = (),
        now: Callable[[], float] = time.monotonic,
        keep_in_memory: bool | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._secrets = tuple(s for s in secrets if s)
        self._now = now
        self._keep = (path is None) if keep_in_memory is None else keep_in_memory
        self.records: list[dict[str, Any]] = []
        self._fh = None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")

    def add_secrets(self, *secrets: str) -> None:
        self._secrets += tuple(s for s in secrets if s)

    def write(self, kind: str, **fields: Any) -> None:
        record = {
            "t": int(self._now() * 1000),
            "wall": _dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": kind,
            **_jsonable(fields),
        }
        record = mask_secrets(record, self._secrets)
        if self._keep:
            self.records.append(record)
        if self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()

    def dump(self, path: Path) -> None:
        """メモリにためた分を書き出す。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class NullLog:
    """ログを取らないときの代用品（log=None を各所で分岐しないため）。"""

    records: list = []

    def write(self, kind: str, **fields: Any) -> None:
        pass

    def add_secrets(self, *secrets: str) -> None:
        pass

    def close(self) -> None:
        pass
