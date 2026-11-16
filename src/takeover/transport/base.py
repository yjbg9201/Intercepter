"""進行役のインターフェース。ここは定義済み（骨組みの契約）。

GameCore はこの Protocol にだけ依存する。実装は local.LocalReferee と csa.CsaTransport。

約束：
- 進行役からの通知は、コンストラクタで受け取った emit コールバックで
  core.events の TransportEvent として行う（GameCore のキューに入る）。
- send_* は「コミット」に当たる。呼んだら取り消せない（I7）。
  GameCore は合法性・手番・宣言条件を確認してから呼ぶ。進行役の側では再確認に頼らない。
- send_move の comment は、エンジンの手のときだけ GameCore が渡す。人間の手は必ず None。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..core.events import TransportEvent
from ..core.types import Color, Score

TransportEmitFn = Callable[[TransportEvent], None]


@runtime_checkable
class Transport(Protocol):
    async def start(self) -> None:
        """接続・対局待ちを始める。GameOffered → GameStarted が emit される。"""
        ...

    async def agree(self) -> None:
        """対局条件を受け入れる（CSA の AGREE）。エンジンの準備ができてから GameCore が呼ぶ。"""
        ...

    async def send_move(
        self,
        color: Color,
        usi: str,
        *,
        score: Score | None = None,
        pv: tuple[str, ...] = (),
        comment: str = "",
    ) -> None:
        """指し手をコミットする。score / pv はエンジンの手のコメント用（人間の手では渡さない）。

        comment は人間が付けた自由コメント（`'...` の形。CSA モードだけがサーバへ送る）。
        """
        ...

    async def send_resign(self, color: Color) -> None: ...

    async def send_declare(self, color: Color) -> None:
        """入玉宣言（CSA の %KACHI）。条件成立を GameCore が確認済みであること。"""
        ...

    async def close(self) -> None:
        """後片付け（CSA は LOGOUT して切断）。何度呼んでもよい。"""
        ...
