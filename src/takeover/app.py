"""組み立てと実行: 設定から各コンポーネントを作って配線し、終了時に片付ける。

担当: M2（ローカル・ヘッドレス＋標準入力コマンド）、M3（CSA）、M4（UI）
参照: SPEC §5.1（全体図）、§6.5（接続の順序・終了処理）

配線（どのモードでも同じ形）:
    EventLog を作る
    → エンジンごとに launch.resolve_launch → EngineController(emit=core.post)
    → Transport(emit=core.post)（LocalReferee か CsaTransport）
    → Seat を組む（ローカル: 両席 LocalSeat。CSA: 自席 LocalSeat＋相手 RemoteSeat。
       CSA は色が Game_Summary で決まるので、席の確定は GameOffered の後）
    → GameCore(on_snapshot=gateway.publish_snapshot, on_notice=gateway.publish_notice)
    → UiGateway(post_command=core.post, get_snapshot=core.snapshot)
    → すべて 1 つのイベントループ上のタスクとして動かす（I3）

終了処理（正常終局・SIGINT / SIGTERM・例外のどれでも必ず通す）:
    棋譜保存 → transport.close() → 全エンジン quit()（quit → wait → kill → wait）→ ログを閉じる
    CSA モードの対局中に SIGINT を受けたら、切断＝負けになることをコンソールに出してから片付ける。

ヘッドレスの標準入力コマンド（M2 の完了条件）:
    take b|w / release b|w / move <usi> / resign b|w / declare b|w / hint on|off
    標準入力は loop.add_reader などで同じループから読む（スレッドを使わない）。
"""

from __future__ import annotations

from pathlib import Path


async def run_local(config_path: Path, *, headless: bool = False) -> int:
    """ローカル対局。戻り値はプロセスの終了コード。"""
    raise NotImplementedError("M2")


async def run_csa(config_path: Path, *, account_alias: str | None, headless: bool = False) -> int:
    """CSA サーバ接続。"""
    raise NotImplementedError("M3")


async def run_fake_server(host: str, port: int) -> int:
    """偽 CSA サーバ（fakes.fake_csa_server.serve_forever）。"""
    from .fakes.fake_csa_server import FakeServerConfig, serve_forever

    await serve_forever(FakeServerConfig(host=host, port=port))
    return 0
