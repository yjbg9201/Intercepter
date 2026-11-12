"""CsaTransport: CSA サーバ（Floodgate / shogi-server）のクライアント（SPEC §6.5）。

担当: M3（テスト: tests/test_csa_transport.py、偽サーバ fakes/fake_csa_server.py。§9 シナリオ 12, 13, 22）

実装で必ず守ること：
- 受信は専用タスク 1 つ。行の解析は csa_parse の純関数。
- 既定では LOGIN に x1 を付けない（付けると自動マッチ登録が効かない）。
- 自分の手番でないときに指し手を送らない（サーバは保留して次の手番に適用してしまう）。
  send_move は GameCore が手番を確認してから呼ぶが、ここでも手番を追跡して違えば例外にする。
- 人間の手（score なしで呼ばれた send_move）にはコメントを付けない。
- 自分の手のエコーは指し手部分の一致を確認する。空行（キープアライブ応答）は無視。
- 終局は「理由行 → 結果行」。結果行で GameEnded を emit。
- キープアライブ: 最後の送信から keepalive_sec（下限 30）経ったら空行を送る。待機中も送る。
- 切断は TransportClosed を emit（自動再接続しない）。
- 送受信した生の行をイベントログへ。LOGIN 行は csa_parse.mask_login で伏せる（I10）。
- テストのために実 Floodgate へ接続しない。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import Color, Score
from .base import TransportEmitFn


@dataclass(frozen=True, slots=True)
class CsaSettings:
    host: str
    port: int
    login_name: str
    login_password: str  # 秘密。ログ・スナップショットに出さない
    x1: bool
    send_engine_comment: bool
    comment_score_pov: str  # "mover" / "black"
    keepalive_sec: int
    connect_timeout_ms: int
    agree_timeout_ms: int


class CsaTransport:
    def __init__(self, settings: CsaSettings, *, emit: TransportEmitFn, log=None) -> None:
        self._settings = settings
        self._emit = emit
        self._log = log

    async def start(self) -> None:
        raise NotImplementedError("M3")

    async def agree(self) -> None:
        raise NotImplementedError("M3")

    async def send_move(
        self, color: Color, usi: str, *, score: Score | None = None, pv: tuple[str, ...] = ()
    ) -> None:
        raise NotImplementedError("M3")

    async def send_resign(self, color: Color) -> None:
        raise NotImplementedError("M3")

    async def send_declare(self, color: Color) -> None:
        raise NotImplementedError("M3")

    async def close(self) -> None:
        raise NotImplementedError("M3")
