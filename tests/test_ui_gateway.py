"""UiGateway（SPEC §6.7、§9 シナリオ 17, 19）。uvicorn を空きポートで起動し、websockets / urllib で接続する。"""

import asyncio
import json
import urllib.error
import urllib.request

import pytest
import websockets

from rig import B, W, Rig
from takeover.core.types import Mode
from takeover.ui import gateway as gw
from takeover.ui.gateway import UiGateway, UiSettings

TOKEN = "s3cret-token"


class UiRig:
    def __init__(self, fake_clock, **settings):
        self.rig = Rig(fake_clock)
        self.settings = UiSettings(port=0, **settings)

    async def __aenter__(self):
        rig = self.rig
        self.gateway = UiGateway(self.settings, post_command=rig.core.post, get_snapshot=rig.core.snapshot)
        rig.core._on_snapshot = self.gateway.publish_snapshot
        rig.core._on_notice = self.gateway.publish_notice
        await rig.start()
        self.core_task = asyncio.create_task(rig.core.run())
        self.serve_task = asyncio.create_task(self.gateway.serve())
        await self.gateway.wait_started()
        self.port = self.gateway.port
        return self

    async def __aexit__(self, *exc):
        await self.gateway.shutdown()
        self.core_task.cancel()
        await asyncio.wait_for(self.serve_task, 10)
        with pytest.raises(asyncio.CancelledError):
            await self.core_task

    def ws_url(self, token=None):
        return f"ws://127.0.0.1:{self.port}/ws" + (f"?token={token}" if token else "")

    def http(self, path="/", host=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if host:
            request.add_header("Host", host)

        def fetch():
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, ""

        return asyncio.to_thread(fetch)


async def recv_type(ws, kind, predicate=lambda m: True):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if message["type"] == kind and predicate(message):
            return message


async def rejected(url, **kwargs):
    try:
        async with websockets.connect(url, **kwargs):
            return False
    except websockets.exceptions.InvalidStatus as exc:
        return exc.response.status_code == 403


async def raw_ws_handshake(port, *, host):
    """Host ヘッダを自由に指定して WebSocket のハンドシェイクを送り、応答の先頭行を返す。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((
        f"GET /ws HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    line = (await asyncio.wait_for(reader.readline(), 5)).decode().strip()
    writer.close()
    return line


async def test_page_and_snapshot_on_connect(fake_clock):
    async with UiRig(fake_clock) as ui:
        status, body = await ui.http("/")
        assert status == 200 and "<title>shogi-takeover</title>" in body
        async with websockets.connect(ui.ws_url()) as ws:
            hello = await recv_type(ws, "hello")
            assert "lan_url" not in hello
            snap = await recv_type(ws, "snapshot")  # 接続直後に最新の状態が届く
            assert snap["phase"] == "playing" and snap["ply"] == 0 and snap["legal_moves"] == []


async def test_two_clients_see_the_same_state_and_commands_are_serialized(fake_clock):
    """PC とスマホの同時接続: どちらの操作も同じキューで処理され、両方に同じ状態が届く。"""
    async with UiRig(fake_clock) as ui:
        async with websockets.connect(ui.ws_url()) as pc, websockets.connect(ui.ws_url()) as phone:
            await recv_type(pc, "snapshot")
            await recv_type(phone, "snapshot")
            await pc.send(json.dumps({"type": "set_mode", "color": "b", "mode": "manual"}))
            a = await recv_type(pc, "snapshot", lambda m: m["seats"]["b"]["mode"] == "manual")
            b = await recv_type(phone, "snapshot", lambda m: m["seats"]["b"]["mode"] == "manual")
            assert len(a["legal_moves"]) == 30 and a["legal_moves"] == b["legal_moves"]
            # 同じ画面（ply=0）から 2 台が別の手を送る → 1 手だけ指され、もう一方は拒否の通知
            await pc.send(json.dumps({"type": "move", "color": "b", "ply": 0, "usi": "7g7f"}))
            await phone.send(json.dumps({"type": "move", "color": "b", "ply": 0, "usi": "2g2f"}))
            a = await recv_type(pc, "snapshot", lambda m: m["ply"] == 1)
            b = await recv_type(phone, "snapshot", lambda m: m["ply"] == 1)
            assert a["sfen"] == b["sfen"] and a["moves"][0]["actor"] == "human"
            notice = await recv_type(phone, "notice")
            assert "古い画面" in notice["text"]
            assert len(ui.rig.usi()) == 1
            # 壊れたメッセージは送った接続にだけ通知され、状態は変わらない
            await pc.send("{broken")
            await recv_type(pc, "notice", lambda m: "解釈できない" in m["text"])
        for _ in range(200):
            if ui.gateway.client_count == 0:
                break
            await asyncio.sleep(0.01)
        assert ui.gateway.client_count == 0


async def test_reconnect_restores_full_state(fake_clock):
    async with UiRig(fake_clock) as ui:
        await ui.rig.engine_move(B, "7g7f")
        async with websockets.connect(ui.ws_url()) as ws:
            snap = await recv_type(ws, "snapshot")
            assert snap["ply"] == 1 and snap["moves"][0]["label"] == "▲７六歩" and snap["last_move"] == "7g7f"


async def test_foreign_origin_and_host_are_rejected(fake_clock):
    """シナリオ 19。"""
    async with UiRig(fake_clock) as ui:
        assert await rejected(ui.ws_url(), origin="http://evil.example")
        assert await rejected(ui.ws_url(), origin=f"http://localhost:{ui.port}")  # Host と別の名前
        assert (await raw_ws_handshake(ui.port, host="evil.example")).startswith("HTTP/1.1 403")
        assert (await raw_ws_handshake(ui.port, host=f"127.0.0.1:{ui.port}")).startswith("HTTP/1.1 101")
        assert (await ui.http("/", host="evil.example"))[0] == 403
        # 自分自身からの接続は通る
        async with websockets.connect(ui.ws_url(), origin=f"http://127.0.0.1:{ui.port}") as ws:
            await recv_type(ws, "snapshot")
        assert ui.rig.usi() == [] and ui.rig.seats[B].mode is Mode.AUTO


async def test_no_http_endpoint_changes_state(fake_clock):
    async with UiRig(fake_clock) as ui:
        for path in ("/move", "/set_mode", "/api/resign", "/docs", "/openapi.json"):
            assert (await ui.http(path))[0] == 404


async def test_lan_mode_requires_token(fake_clock):
    """シナリオ 17: トークンなし・誤トークンの HTTP / WebSocket は拒否。"""
    async with UiRig(fake_clock, host="127.0.0.1", lan_enabled=True, token=TOKEN) as ui:
        assert (await ui.http("/"))[0] == 403
        assert (await ui.http("/?token=wrong"))[0] == 403
        assert (await ui.http(f"/?token={TOKEN}"))[0] == 200
        assert await rejected(ui.ws_url())
        assert await rejected(ui.ws_url("wrong"))
        async with websockets.connect(ui.ws_url(TOKEN)) as ws:
            hello = await recv_type(ws, "hello")
            assert TOKEN in hello["lan_url"]  # ループバックからの接続にだけ見せる
            snap = await recv_type(ws, "snapshot")
            assert TOKEN not in json.dumps(snap)


def test_settings_refuse_unsafe_bind():
    with pytest.raises(ValueError):
        UiSettings(host="0.0.0.0")  # LAN 無効で公開バインド
    with pytest.raises(ValueError):
        UiSettings(host="0.0.0.0", lan_enabled=True, token="")  # トークンなしの LAN 公開


def test_host_and_origin_rules():
    assert gw.is_allowed_host("localhost", ()) and gw.is_allowed_host("192.168.1.20", ())
    assert gw.is_allowed_host("mypc.lan", ("mypc.lan",)) and not gw.is_allowed_host("mypc.lan", ())
    assert not gw.is_allowed_host(None, ()) and not gw.is_allowed_host("evil.example", ())
    assert gw.host_of("localhost:8765") == "localhost" and gw.host_of("[::1]:8765") == "::1"
    assert gw.is_same_origin(None, "localhost:8765", ())
    assert gw.is_same_origin("http://localhost:8765", "localhost:8765", ())
    assert not gw.is_same_origin("http://localhost:9999", "localhost:8765", ())
    assert not gw.is_same_origin("null", "localhost:8765", ())
    assert not gw.is_same_origin("file://", "localhost:8765", ())
