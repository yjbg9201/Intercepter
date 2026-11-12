"""偽 CSA サーバ。テストから in-process で起動するほか、`python -m takeover fake-server` でも動く。

担当: M0（最小実装）、M3 で CsaTransport のテストに合わせて拡充
参照: SPEC §9「偽 CSA サーバ」、§6.5、§6.4（時計の規則）

- asyncio.start_server で実装する（port=0 で空きポート）。
- LOGIN → Game_Summary → AGREE → START → `<指し手>,T<n>` のエコー → 終局通知。
- 時計は shogi-server と同じ規則（加算 → 減算、秒未満切り捨て、時間切れ判定）。
- 相手はランダム合法手（seed・遅延を指定可）。台本（指し手の列）で指させることもできる。
- %TORYO / %KACHI（27 点法で判定）/ 千日手 / 最大手数。
- テストから参照できる記録:
    received_lines   … クライアントから受信した全行（コメントを含む生の行）
    deferred_moves   … 手番外に届いて保留した指し手（本物と同じ挙動。誤送信の検出用）
    login            … 受け取った名前とパスワード
- 空行には空行で応答する（キープアライブ）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class FakeServerConfig:
    host: str = "127.0.0.1"
    port: int = 0  # 0 は空きポート
    total_sec: int = 300
    byoyomi_sec: int = 0
    increment_sec: int = 10
    max_moves: int = 256
    client_color: str = "+"  # クライアントに割り当てる手番（"+" / "-"）
    opponent_name: str = "fake_opponent"
    opponent_delay_ms: int = 0
    seed: int = 0


@dataclass(slots=True)
class FakeCsaServer:
    config: FakeServerConfig = field(default_factory=FakeServerConfig)
    received_lines: list[str] = field(default_factory=list)
    deferred_moves: list[str] = field(default_factory=list)

    @property
    def port(self) -> int:
        """実際に待ち受けているポート（start() の後で有効）。"""
        raise NotImplementedError("M0")

    async def start(self) -> None:
        raise NotImplementedError("M0")

    async def wait_game_end(self) -> None:
        raise NotImplementedError("M0")

    async def close(self) -> None:
        raise NotImplementedError("M0")


async def serve_forever(config: FakeServerConfig) -> None:
    """CLI（`python -m takeover fake-server`）用。Ctrl-C まで対局を受け付け続ける。"""
    raise NotImplementedError("M0")
