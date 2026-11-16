"""CSA モードのセッション（M7。SPEC §6.5、§6.10）: 名前ストックの編集、接続画面、連続参加。

偽 CSA サーバ ＋ 偽エンジン（子プロセス）＋ 実際の UiGateway（空きポート）で、アプリ全体を動かす。
"""

import asyncio
import io
import json
import os
import socket
import stat
import sys
import tomllib

import pytest
import websockets

from takeover import accounts as acc
from takeover import app
from takeover.accounts import Account, AccountError
from takeover.core import events as ev
from takeover.fakes.fake_csa_server import FakeCsaServer, FakeServerConfig
from takeover.ui import messages as m

SECRET = "TRIPSECRET"
NEW_SECRET = "NEWTRIP99"


# ---- 名前ストックの編集（純関数とファイル） ------------------------------- #

BASE = [Account(alias="a1", name="first", password="pw1", memo="メモ"),
        Account(alias="a2", name="second_human", game="floodgate-300-10F", trip=SECRET)]


def test_upsert_adds_edits_and_keeps_secrets():
    added = acc.upsert_account(BASE, alias="a3", name="third", game="g", trip="t3")
    assert [a.alias for a in added] == ["a1", "a2", "a3"] and len(BASE) == 2  # 元の列は変えない
    # 編集: trip / password を空で送ると元の値を保つ（UI には秘密を出さないので「変更なし」の意味）
    edited = acc.upsert_account(BASE, alias="b2", name="renamed", game="floodgate-600-10F", original_alias="a2")
    assert [a.alias for a in edited] == ["a1", "b2"]
    assert (edited[1].name, edited[1].game, edited[1].trip) == ("renamed", "floodgate-600-10F", SECRET)
    replaced = acc.upsert_account(BASE, alias="a2", name="second_human", game="g", trip="NEW", original_alias="a2")
    assert replaced[1].trip == "NEW"
    for bad in (dict(alias="a1", name="dup", password="x"),  # alias の重複
                dict(alias="x", name="bad name", password="x"),  # LOGIN 名に空白
                dict(alias="x", name="n"),  # 秘密なし
                dict(alias="x", name="n", password="x", original_alias="zzz")):  # 編集対象がない
        with pytest.raises(AccountError):
            acc.upsert_account(BASE, **bad)


def test_delete_and_find():
    assert [a.alias for a in acc.delete_account(BASE, "a1")] == ["a2"]
    with pytest.raises(AccountError):
        acc.delete_account(BASE, "zzz")
    assert acc.find_account(BASE, "a2").name == "second_human" and acc.find_account(BASE, None) is None


def test_save_roundtrip_permissions_and_last_alias(tmp_path):
    path = tmp_path / "accounts.toml"
    tricky = [*BASE, Account(alias="q", name="n", password='p"w\\x', memo='引用 " と \\ と # を含む')]
    acc.save_accounts(path, tricky)
    assert acc.load_accounts(path) == tricky  # 書いたものがそのまま読める
    assert tomllib.loads(path.read_text(encoding="utf-8"))["account"][2]["password"] == 'p"w\\x'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # 秘密を含むので本人だけ
    assert not (tmp_path / "accounts.toml.tmp").exists()
    assert acc.load_last_alias(path) is None
    acc.save_last_alias(path, "a2")
    assert acc.load_last_alias(path) == "a2"
    assert SECRET not in acc.state_path(path).read_text(encoding="utf-8")
    acc.state_path(path).write_text("{broken", encoding="utf-8")
    assert acc.load_last_alias(path) is None


# ---- メッセージ ------------------------------------------------------------ #


def test_session_messages_parse_and_hide_secrets():
    def parse(data):
        return m.parse_client_message(json.dumps(data), client_id="c1")

    assert parse({"type": "connect", "alias": "a1"}) == ev.ConnectCommand(alias="a1", client_id="c1")
    assert parse({"type": "disconnect"}) == ev.DisconnectCommand(client_id="c1")
    assert parse({"type": "set_continuous", "enabled": True}) == ev.SetContinuousCommand(enabled=True, client_id="c1")
    assert parse({"type": "delete_account", "alias": "a1"}) == ev.DeleteAccountCommand(alias="a1", client_id="c1")
    saved = parse({"type": "save_account", "alias": " a3 ", "name": "n", "game": "g", "trip": SECRET,
                   "original_alias": "a2"})
    assert (saved.alias, saved.name, saved.game, saved.trip, saved.original_alias) == ("a3", "n", "g", SECRET, "a2")
    assert SECRET not in repr(saved)  # ログに repr を書いても秘密が出ない
    bad = [{"type": "connect"}, {"type": "connect", "alias": 3}, {"type": "set_continuous", "enabled": "on"},
           {"type": "save_account", "alias": "", "name": "n"}, {"type": "save_account", "alias": "a", "name": 1},
           {"type": "save_account", "alias": "a", "name": "n", "trip": "x" * 300},
           {"type": "save_account", "alias": "a", "name": "n", "original_alias": 5}, {"type": "delete_account"}]
    assert all(parse(b) is None for b in bad)
    assert m.session_message({"state": "lobby"}) == {"type": "session", "state": "lobby"}


# ---- アプリ全体 ------------------------------------------------------------ #


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp_path, server_port, *, ui_port=None, extra_csa=""):
    args = ", ".join(json.dumps(a) for a in ("-m", "takeover.fakes.fake_engine", "--seed", "4"))
    ui = f"[ui]\nport = {ui_port}\n" if ui_port else ""
    (tmp_path / "config.toml").write_text(
        f'{ui}[engine.main]\npath = {json.dumps(sys.executable)}\nargs = [{args}]\n'
        f'[csa]\nhost = "127.0.0.1"\nport = {server_port}\nengine = "main"\ndefault_account = "a1"\n'
        f'rejoin_delay_sec = 0\n{extra_csa}'
        '[record]\nformats = ["csa", "kif"]\n[policy]\nquit_timeout_ms = 2000\n', encoding="utf-8")
    (tmp_path / "accounts.toml").write_text(
        '[[account]]\nalias = "a1"\nname = "first"\npassword = "pw1"\n'
        f'[[account]]\nalias = "a2"\nname = "second_human"\ngame = "floodgate-300-10F"\ntrip = "{SECRET}"\n',
        encoding="utf-8")
    return tmp_path / "config.toml"


async def until(predicate, timeout=30):
    async def loop():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def test_continuous_play_headless_rejoins_until_turned_off(tmp_path):
    """連続参加: 終局のたびに棋譜保存 → LOGOUT → 再 LOGIN。オフにすると、いまの対局のあとで終わる。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6))
    await server.start()
    read_fd, write_fd = os.pipe()
    try:
        config = write_config(tmp_path, server.port)
        out = io.StringIO()
        with os.fdopen(read_fd, "rb", buffering=0) as stdin:
            task = asyncio.create_task(app.run_csa(config, account_alias="a2", headless=True, stdin=stdin,
                                                   out=out, continuous=True))
            await until(lambda: len(list((tmp_path / "records").glob("*.csa"))) >= 2)
            os.write(write_fd, b"continuous off\n")
            code = await asyncio.wait_for(task, 30)
        assert code == 0, out.getvalue()
        records = sorted((tmp_path / "records").glob("*.csa"))
        assert len(records) >= 2 and len(set(records)) == len(records)  # 1 局ごとに別のファイル
        assert len(list((tmp_path / "records").glob("*.kif"))) == len(records)  # 設定した形式ぶん出る
        assert len(server.logins) == len(records)  # 対局のたびに LOGIN し直している
        assert set(server.logins) == {("second_human", f"floodgate-300-10F,{SECRET}", False)}
        assert server.received_lines.count("LOGOUT") == len(records) and server.deferred_moves == []
        text = out.getvalue()
        assert text.count("[終局]") == len(records) and "連続参加をオフにしました" in text
        (log,) = (tmp_path / "logs").glob("*.jsonl")
        assert SECRET not in log.read_text(encoding="utf-8")
    finally:
        os.close(write_fd)
        await server.close()


async def test_continuous_play_stops_after_login_failure(tmp_path):
    """接続・LOGIN に失敗したら、連続参加でも繰り返し接続しない。"""
    server = FakeCsaServer(FakeServerConfig(accept_login=False))
    await server.start()
    try:
        config = write_config(tmp_path, server.port)
        out = io.StringIO()
        code = await asyncio.wait_for(
            app.run_csa(config, account_alias="a1", headless=True, stdin=open(os.devnull), out=out,
                        continuous=True), 30)
        assert code == 1 and len(server.logins) == 1 and "LOGIN" in out.getvalue()
        assert not list((tmp_path / "records").glob("*"))  # 対局していないので棋譜は作らない
    finally:
        await server.close()


class Ui:
    """WebSocket で UI の代わりをする。受け取ったメッセージを種類ごとに覚える。"""

    def __init__(self, port):
        self.url = f"ws://127.0.0.1:{port}/ws"
        self.raw: list[str] = []
        self.session = None
        self.snapshot = None
        self.notices: list[str] = []

    async def __aenter__(self):
        for _ in range(200):  # UI の起動を待つ
            try:
                self.ws = await websockets.connect(self.url)
                break
            except OSError:
                await asyncio.sleep(0.05)
        self.task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        await self.ws.close()

    async def _pump(self):
        async for text in self.ws:
            self.raw.append(text)
            message = json.loads(text)
            if message["type"] == "session":
                self.session = message
            elif message["type"] == "snapshot":
                self.snapshot = message
            elif message["type"] == "notice":
                self.notices.append(message["text"])

    async def send(self, **message):
        await self.ws.send(json.dumps(message))

    async def state(self, name, timeout=30):
        await until(lambda: self.session is not None and self.session["state"] == name, timeout)


async def test_lobby_edit_accounts_connect_and_return(tmp_path):
    """UI の接続画面: 名前の追加・編集・削除 → 選んで接続 → 終局後は接続画面に戻り、もう一度接続できる。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port)
    out = io.StringIO()
    task = asyncio.create_task(app.run_csa(config, account_alias=None, out=out))
    try:
        async with Ui(ui_port) as ui:
            await ui.state("lobby")
            assert server.logins == []  # 名前を選ぶまで接続しない
            assert ui.session["accounts"] == [
                {"alias": "a1", "name": "first", "game": "", "memo": ""},
                {"alias": "a2", "name": "second_human", "game": "floodgate-300-10F", "memo": ""}]
            assert ui.session["selected"] == "a1" and ui.session["continuous"] is False  # default_account
            assert ui.snapshot is None

            await ui.send(type="save_account", alias="a3", name="third_human", game="floodgate-300-10F",
                          trip=NEW_SECRET, memo="UI から追加")
            await until(lambda: len(ui.session["accounts"]) == 3)
            await ui.send(type="save_account", alias="a2", name="second_x", game="floodgate-300-10F",
                          original_alias="a2")  # 秘密は空のまま（元の trip を保つ）
            await until(lambda: ui.session["accounts"][1]["name"] == "second_x")
            await ui.send(type="delete_account", alias="a1")
            await until(lambda: len(ui.session["accounts"]) == 2)
            await ui.send(type="save_account", alias="bad", name="has space", password="x")
            await until(lambda: any("保存できません" in n for n in ui.notices))
            stored = acc.load_accounts(tmp_path / "accounts.toml")
            assert [(a.alias, a.name, a.trip) for a in stored] == [
                ("a2", "second_x", SECRET), ("a3", "third_human", NEW_SECRET)]

            await ui.send(type="connect", alias="a3")
            await until(lambda: ui.snapshot is not None and ui.snapshot["phase"] == "ended")
            await ui.state("lobby")
            assert server.logins == [("third_human", f"floodgate-300-10F,{NEW_SECRET}", False)]
            assert ui.snapshot["result"] == "black_win"  # 終局図は接続画面に戻っても見えている
            assert len(list((tmp_path / "records").glob("*.csa"))) == 1
            assert ui.session["selected"] == "a3" and ui.session["account"] is None
            assert acc.load_last_alias(tmp_path / "accounts.toml") == "a3"  # 最後に使った名前が既定になる

            await ui.send(type="connect", alias="a2")  # 同じ起動のまま、別の名前でもう 1 局
            await until(lambda: len(server.logins) == 2)
            await until(lambda: len(list((tmp_path / "records").glob("*.csa"))) == 2)
            await ui.state("lobby")
            assert server.logins[1][0] == "second_x"
            await ui.send(type="connect", alias="zzz")
            await until(lambda: any("名前ストックにありません" in n for n in ui.notices))

            everything = "\n".join(ui.raw)
            assert SECRET not in everything and NEW_SECRET not in everything  # UI へ秘密を送らない
        (log,) = (tmp_path / "logs").glob("*.jsonl")
        logged = log.read_text(encoding="utf-8")
        assert SECRET not in logged and NEW_SECRET not in logged  # UI から入れた秘密もログで伏せる
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()


async def test_disconnect_while_waiting_and_continuous_toggle_from_ui(tmp_path):
    """対局待ちの間は UI から切断できる。連続参加は UI から切り替えられ、次の接続まで待つ間にもやめられる。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6, match_delay_sec=60))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port, extra_csa="")
    out = io.StringIO()
    task = asyncio.create_task(app.run_csa(config, account_alias="a1", out=out))
    try:
        async with Ui(ui_port) as ui:
            await ui.state("waiting")  # LOGIN 済みで、まだ対局が決まっていない
            assert ui.session["account"] == "a1"
            await ui.send(type="disconnect")
            await ui.state("lobby")
            await until(lambda: "LOGOUT" in server.received_lines)
            assert not list((tmp_path / "records").glob("*"))  # 対局していないので棋譜は作らない

            server.config.match_delay_sec = 0  # 以後はすぐ対局が決まる
            await ui.send(type="set_continuous", enabled=True)
            await until(lambda: ui.session["continuous"] is True)
            await ui.send(type="connect", alias="a1")
            await until(lambda: len(list((tmp_path / "records").glob("*.csa"))) >= 2)  # 自動で 2 局目へ
            assert len(server.logins) >= 3
            server.config.match_delay_sec = 60
            await ui.send(type="set_continuous", enabled=False)
            await until(lambda: ui.session["continuous"] is False)
            # 進行中の対局が終わるか、対局待ちなら切断して、接続画面に戻る
            for _ in range(200):
                if ui.session["state"] == "lobby":
                    break
                if ui.session["state"] == "waiting":
                    await ui.send(type="disconnect")
                await asyncio.sleep(0.05)
            await ui.state("lobby")
            logins = len(server.logins)
            await asyncio.sleep(0.3)
            assert len(server.logins) == logins  # オフにしたあとは自動で接続しない
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()


async def test_login_failure_returns_to_lobby_with_reason(tmp_path):
    server = FakeCsaServer(FakeServerConfig(accept_login=False))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port)
    task = asyncio.create_task(app.run_csa(config, account_alias="a1", out=io.StringIO()))
    try:
        async with Ui(ui_port) as ui:
            await until(lambda: len(server.logins) == 1)
            await ui.state("lobby")
            assert "LOGIN" in ui.session["message"]  # 失敗の理由を接続画面に出す
            await asyncio.sleep(0.3)
            assert len(server.logins) == 1  # 自動では再接続しない
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()


async def test_ui_can_start_without_accounts_file(tmp_path):
    """名前ストックがまだなくても UI は起動し、接続画面から最初の名前を登録できる。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=4))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port)
    (tmp_path / "accounts.toml").unlink()
    task = asyncio.create_task(app.run_csa(config, account_alias=None, out=io.StringIO()))
    try:
        async with Ui(ui_port) as ui:
            await ui.state("lobby")
            assert ui.session["accounts"] == [] and ui.session["selected"] is None
            await ui.send(type="save_account", alias="n1", name="newcomer", password="pw")
            await until(lambda: len(ui.session["accounts"]) == 1)
            await ui.send(type="connect", alias="n1")
            await until(lambda: server.logins == [("newcomer", "pw", False)])
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()
    # ヘッドレスでは、名前ストックがなければ従来どおりエラーで終わる
    (tmp_path / "accounts.toml").unlink()
    out = io.StringIO()
    assert await app.run_csa(config, account_alias=None, headless=True, out=out) == 1
    assert "accounts.example.toml" in out.getvalue()
