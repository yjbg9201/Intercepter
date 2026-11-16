"""UiGateway: HTTP と WebSocket の入口（SPEC §6.7）。

- 状態を持たない。受け取ったメッセージを ui.messages で Command にして GameCore へ渡し、
  GameCore から来た Snapshot / notice を全接続へ配るだけ（I1, I8）。合法手判定をしない。
- 新しい接続には、まず最新のスナップショットを 1 回送る（再読み込みで完全に復元できる）。
- engine_info は最大 5 回/秒に間引く。
- WebSocket のハンドシェイクで Origin を検査する。HTTP は Host を検査する（v0 から）。
- 状態を変える HTTP エンドポイントはない（変更は WebSocket のコマンドだけ）。
- 既定のバインドは 127.0.0.1。0.0.0.0 にするのは ui.lan.enabled のときだけで、そのときは
  トークン必須（UiSettings がトークンなしの LAN 公開を拒否する）。
- トークンはログ・スナップショットに出さない。比較は hmac.compare_digest。
- uvicorn は同じイベントループ上で動かす（スレッドを使わない。I3）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import itertools
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..core.events import Command, EngineInfoEvent
from ..core.snapshot import Snapshot
from ..core.types import Color
from . import messages

STATIC_DIR = Path(__file__).resolve().parent / "static"
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
INFO_INTERVAL_SEC = 0.2  # engine_info は最大 5 回/秒
QUEUE_LIMIT = 200  # 1 接続あたりの未送信メッセージの上限（超えたら切断）


@dataclass(frozen=True, slots=True)
class UiSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    lan_enabled: bool = False
    token: str = ""  # 秘密。lan_enabled のとき必須
    allowed_hosts: tuple[str, ...] = ()  # Origin / Host の許可リスト（localhost と IP 直指定は常に許可）
    notify_my_turn: bool = True

    def __post_init__(self) -> None:
        if self.lan_enabled and not self.token:
            raise ValueError("LAN 公開にはトークンが必要です")
        if not self.lan_enabled and self.host not in _LOCAL_NAMES:
            raise ValueError("LAN 公開（ui.lan.enabled）なしで 127.0.0.1 以外にはバインドできません")


def host_of(header: str | None) -> str | None:
    """`Host` ヘッダや Origin の netloc から、ポートを除いたホスト名を取り出す（純関数）。"""
    if not header:
        return None
    try:
        return urlsplit("//" + header).hostname
    except ValueError:
        return None


def is_allowed_host(hostname: str | None, allowed: tuple[str, ...]) -> bool:
    """localhost・IP アドレスの直指定・許可リストのどれかなら True（純関数）。

    DNS リバインディングは「攻撃者のドメイン名」でアクセスさせる手口なので、名前は許可リスト制にする。
    """
    if not hostname:
        return False
    hostname = hostname.lower()
    if hostname in _LOCAL_NAMES or hostname in {h.lower() for h in allowed}:
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def is_same_origin(origin: str | None, host_header: str | None, allowed: tuple[str, ...]) -> bool:
    """WebSocket の Origin 検査（純関数）。Origin がない（ブラウザ以外）は通す。"""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return parts.netloc.lower() == (host_header or "").lower() and is_allowed_host(parts.hostname, allowed)


def lan_address() -> str | None:
    """LAN 側の自分の IP アドレス（パケットは送らない）。分からなければ None。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
    except OSError:
        return None


class _Client:
    def __init__(self, client_id: str) -> None:
        self.id = client_id
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()


class UiGateway:
    def __init__(
        self,
        settings: UiSettings,
        *,
        post_command: Callable[[Command], None],
        get_snapshot: Callable[[], Snapshot],
        get_session: Callable[[], dict] | None = None,  # CSA モードのセッション表示（接続画面など）
        get_backlog: Callable[[], list[dict]] | None = None,  # 新しい接続に最初に送る追加メッセージ（通信ログなど）
        now: Callable[[], float] = time.monotonic,
        out=None,
    ) -> None:
        self._settings = settings
        self._post_command = post_command
        self._get_snapshot = get_snapshot
        self._get_session = get_session
        self._get_backlog = get_backlog
        self._now = now
        self._out = out
        self._clients: dict[str, _Client] = {}
        self._ids = itertools.count(1)
        self._last_snapshot: Snapshot | None = None
        self._last_info: dict[tuple, float] = {}
        self._server: uvicorn.Server | None = None
        self._socket: socket.socket | None = None
        self._started = asyncio.Event()
        self.port: int | None = None
        self.app = self._build_app()

    # ------------------------------------------------------------------ #
    # 認証・検査
    # ------------------------------------------------------------------ #

    def _token_ok(self, token: str | None) -> bool:
        if not self._settings.lan_enabled:
            return True
        return bool(token) and hmac.compare_digest(token.encode(), self._settings.token.encode())

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        settings = self._settings

        @app.middleware("http")
        async def check_host(request: Request, call_next):
            if not is_allowed_host(host_of(request.headers.get("host")), settings.allowed_hosts):
                return PlainTextResponse("forbidden host", status_code=403)
            return await call_next(request)

        @app.get("/")
        async def index(request: Request):
            if not self._token_ok(request.query_params.get("token")):
                return PlainTextResponse("token required", status_code=403)
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            headers = websocket.headers
            host_header = headers.get("host")
            if (not is_allowed_host(host_of(host_header), settings.allowed_hosts)
                    or not is_same_origin(headers.get("origin"), host_header, settings.allowed_hosts)
                    or not self._token_ok(websocket.query_params.get("token"))):
                await websocket.close(code=1008)  # accept 前なのでハンドシェイクが 403 で失敗する
                return
            await websocket.accept()
            await self._serve_client(websocket)

        return app

    # ------------------------------------------------------------------ #
    # 接続ごとの処理
    # ------------------------------------------------------------------ #

    async def _serve_client(self, websocket: WebSocket) -> None:
        client = _Client(f"ui{next(self._ids)}")
        self._clients[client.id] = client
        hello = {"type": "hello", "client_id": client.id, "notify_my_turn": self._settings.notify_my_turn}
        peer = websocket.client.host if websocket.client else ""
        if self._settings.lan_enabled and peer in _LOCAL_NAMES:
            hello["lan_url"] = self.lan_url()  # PC 側の画面にだけ、スマホ用の URL を見せる
        client.queue.put_nowait(messages.dumps(hello))
        if self._get_session is not None:
            client.queue.put_nowait(messages.dumps(messages.session_message(self._get_session())))
        if self._get_backlog is not None:
            for message in self._get_backlog():
                client.queue.put_nowait(messages.dumps(message))
        try:
            client.queue.put_nowait(messages.dumps(messages.snapshot_message(self._get_snapshot())))
        except Exception:
            pass  # まだ対局の準備ができていない

        async def sender() -> None:
            while True:
                text = await client.queue.get()
                if text is None:
                    await websocket.close(code=1013)
                    return
                await websocket.send_text(text)

        send_task = asyncio.create_task(sender())
        try:
            while True:
                raw = await websocket.receive_text()
                command = messages.parse_client_message(raw, client_id=client.id)
                if command is None:
                    client.queue.put_nowait(messages.dumps(messages.notice_message("解釈できない操作を無視しました")))
                else:
                    self._post_command(command)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.CancelledError:
            pass  # アプリの終了で接続ごと止められた（uvicorn にトレースバックを出させない）
        finally:
            self._clients.pop(client.id, None)
            send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task

    def _broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        text = messages.dumps(message)
        for client in list(self._clients.values()):
            if client.queue.qsize() >= QUEUE_LIMIT:
                # 受け取りが追いつかない接続は切る（再接続すれば最新のスナップショットで復元できる）
                self._clients.pop(client.id, None)
                client.queue.put_nowait(None)
                continue
            client.queue.put_nowait(text)

    # ---- GameCore から呼ばれる（ブロックしない） ----

    def publish_snapshot(self, snapshot: Snapshot) -> None:
        self._last_snapshot = snapshot
        self._broadcast(messages.snapshot_message(snapshot))

    def publish_notice(self, text: str) -> None:
        self._broadcast(messages.notice_message(text))

    def publish_message(self, message: dict) -> None:
        """そのまま全接続へ配る（通信ログなど。秘密を含めないのは呼び出し側の責任）。"""
        self._broadcast(message)

    def publish_session(self) -> None:
        if self._get_session is not None:
            self._broadcast(messages.session_message(self._get_session()))

    def publish_engine_info(self, color: Color, kind: str, info: EngineInfoEvent) -> None:
        key = (color, kind, info.multipv)
        now = self._now()
        last = self._last_info.get(key)
        if last is not None and now - last < INFO_INTERVAL_SEC:
            return
        self._last_info[key] = now
        snapshot = self._last_snapshot
        view = messages.viewpoint(snapshot) if snapshot else Color.BLACK
        self._broadcast(messages.engine_info_message(
            color, kind, info, view=view, sfen=snapshot.sfen if snapshot else None))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ------------------------------------------------------------------ #
    # 起動・停止
    # ------------------------------------------------------------------ #

    def local_url(self) -> str:
        query = f"/?token={self._settings.token}" if self._settings.lan_enabled else "/"
        return f"http://localhost:{self.port}{query}"

    def lan_url(self) -> str | None:
        if not self._settings.lan_enabled:
            return None
        address = lan_address() or "<このPCのIPアドレス>"
        return f"http://{address}:{self.port}/?token={self._settings.token}"

    async def serve(self) -> None:
        """uvicorn を現在のイベントループで起動し、停止されるまで動かす。"""
        s = self._settings
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((s.host if s.host != "localhost" else "127.0.0.1", s.port))
        sock.listen(64)
        sock.setblocking(False)
        self._socket = sock
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", access_log=False, lifespan="off",
                                ws_max_size=messages.MAX_MESSAGE_BYTES * 4)
        server = uvicorn.Server(config)
        server.capture_signals = contextlib.nullcontext  # 信号は app.py が扱う
        self._server = server
        if self._out is not None:
            print(f"UI: {self.local_url()}", file=self._out, flush=True)
            if s.lan_enabled:
                print(f"スマホから: {self.lan_url()}（同じ LAN 内のみ。WSL2 では SPEC §11 の設定が必要）",
                      file=self._out, flush=True)
        self._started.set()
        try:
            await server.serve(sockets=[sock])
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    async def wait_started(self) -> None:
        await self._started.wait()
        while self._server is not None and not self._server.started:
            await asyncio.sleep(0.01)

    async def shutdown(self) -> None:
        for client in list(self._clients.values()):
            client.queue.put_nowait(None)
        if self._server is not None:
            self._server.should_exit = True


def build_gateway(config, *, post_command, get_snapshot, get_session=None, get_backlog=None, out=None) -> UiGateway:
    """設定から UiGateway を作る。LAN 公開でトークンが空なら、ここで生成する。"""
    ui = config.ui
    token = ui.lan.token
    if ui.lan.enabled and not token:
        token = secrets.token_urlsafe(16)
    host = "0.0.0.0" if ui.lan.enabled else ui.host
    settings = UiSettings(host=host, port=ui.port, lan_enabled=ui.lan.enabled, token=token,
                          allowed_hosts=tuple(ui.lan.allowed_hosts), notify_my_turn=ui.notify_my_turn)
    return UiGateway(settings, post_command=post_command, get_snapshot=get_snapshot,
                     get_session=get_session, get_backlog=get_backlog, out=out)
