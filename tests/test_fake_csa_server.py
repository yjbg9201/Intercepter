"""偽 CSA サーバの最小動作（M0）。素のソケットで話しかけて確かめる。"""

import asyncio

from takeover.fakes.fake_csa_server import FakeCsaServer, FakeServerConfig
from takeover.transport import csa_parse


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, server):
        return cls(*await asyncio.open_connection("127.0.0.1", server.port))

    def send(self, line):
        self.writer.write((line + "\n").encode())

    async def line(self):
        raw = await asyncio.wait_for(self.reader.readline(), 10)
        assert raw, "EOF"
        return raw.decode().rstrip("\n")

    async def until(self, predicate):
        lines = []
        while True:
            lines.append(await self.line())
            if predicate(lines[-1]):
                return lines

    async def login_and_start(self):
        self.send("LOGIN tester game,trip")
        assert await self.line() == "LOGIN:tester OK"
        summary = await self.until(lambda l: l == "END Game_Summary")
        self.send("AGREE")
        assert (await self.line()).startswith("START:")
        return csa_parse.parse_game_summary(summary)


async def serve(**kwargs):
    server = FakeCsaServer(FakeServerConfig(**kwargs))
    await server.start()
    return server


async def test_login_summary_move_echo_and_resign():
    server = await serve(opponent_moves=["3c3d"])
    try:
        client = await Client.connect(server)
        summary = await client.login_and_start()
        assert summary.my_color.value == "b" and summary.time_control[summary.my_color].increment_ms == 10000
        client.send("+7776FU,'* 30 -3334FU")
        assert await client.line() == "+7776FU,T0"  # エコーにコメントは含まれない
        assert await client.line() == "-3334FU,T0"
        client.send("")  # キープアライブ
        assert await client.line() == ""
        client.send("%TORYO")
        assert await client.line() == "%TORYO,T0"
        assert [await client.line(), await client.line()] == ["#RESIGN", "#LOSE"]
        client.send("LOGOUT")
        await server.wait_game_end(5)
        assert server.login == ("tester", "game,trip", False)
        assert "+7776FU,'* 30 -3334FU" in server.received_lines
        assert server.moves == ["7g7f", "3c3d"] and server.deferred_moves == []
    finally:
        await server.close()


async def test_illegal_move_loses_and_bad_declaration_loses():
    for bad in ("+7775FU", "%KACHI"):
        server = await serve()
        try:
            client = await Client.connect(server)
            await client.login_and_start()
            client.send(bad)
            lines = await client.until(lambda l: l in ("#WIN", "#LOSE"))
            assert lines[-2:] == ["#ILLEGAL_MOVE", "#LOSE"]
        finally:
            await server.close()


async def test_out_of_turn_move_is_deferred_like_real_server():
    server = await serve(opponent_delay_ms=200, opponent_moves=["3c3d"])
    try:
        client = await Client.connect(server)
        await client.login_and_start()
        client.send("+7776FU")
        assert await client.line() == "+7776FU,T0"
        client.send("+2726FU")  # 相手の手番中に送ってしまう
        assert await client.line() == "-3334FU,T0"
        assert await client.line() == "+2726FU,T0"  # 捨てられず、次の手番に適用される
        assert server.deferred_moves == ["+2726FU"]
    finally:
        await server.close()


async def test_client_as_white_and_opponent_resigns():
    server = await serve(client_color="-", opponent_resign_after=2)
    try:
        client = await Client.connect(server)
        summary = await client.login_and_start()
        assert summary.my_color.value == "w"
        first = await client.line()
        assert first.startswith("+") and first.endswith(",T0")
        client.send("-3334FU")
        assert await client.line() == "-3334FU,T0"
        lines = await client.until(lambda l: l in ("#WIN", "#LOSE"))
        assert lines == ["%TORYO,T0", "#RESIGN", "#WIN"]
    finally:
        await server.close()


async def test_login_rejected():
    server = await serve(accept_login=False)
    try:
        client = await Client.connect(server)
        client.send("LOGIN tester pw")
        assert await client.line() == "LOGIN:incorrect"
    finally:
        await server.close()
