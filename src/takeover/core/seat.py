"""席（SPEC §6.2）。ここは定義済み（骨組みの契約）。

Seat はデータの入れ物で、振る舞い（いつ何をエンジンに頼むか）は GameCore が持つ。
エンジンは席ごとに別プロセス（先後に同じ設定名を指定しても共有しない）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.base import Engine
from .types import Color, Mode, SeatKind


@dataclass(slots=True)
class LocalSeat:
    """奪取・返却できる席。engine が None なら人間専用席（返却不可）。"""

    color: Color
    name: str
    engine: Engine | None
    mode: Mode = Mode.AUTO
    ponder_enabled: bool = False

    kind = SeatKind.LOCAL


@dataclass(slots=True)
class RemoteSeat:
    """CSA の相手。モードを持たず、奪取できない。"""

    color: Color
    name: str

    kind = SeatKind.REMOTE


Seat = LocalSeat | RemoteSeat
