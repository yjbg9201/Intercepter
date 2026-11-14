"""CsaTransport と CSA モードの対局（SPEC §6.5、§9 シナリオ 12, 13, 22）。偽 CSA サーバを使う。"""

import asyncio
import io
import json
import os
import random
import sys

import cshogi
import pytest

from takeover import app
from takeover.core import events as ev
from takeover.core.game import GameCore, GamePolicy
from takeover.core.seat import LocalSeat
from takeover.core.types import Actor, Color, EndReason, GameResult, Mode, Phase, Score
from takeover.fakes.fake_csa_server import FakeCsaServer, FakeServerConfig
from takeover.fakes.scripted_engine import ScriptedEngine
from takeover.record import kifu
from takeover.record.eventlog import EventLog
from takeover.transport.csa import CsaSettings, CsaTransport, TransportError

SECRET = "TRIPSECRET"


def random_reply(seed=5, cp=120):
    rng = random.Random(seed)

    def reply(position_cmd):
        board = cshogi.Board()
        board.set_position(position_cmd[len("position "):])
        legal = sorted(cshogi.move_to_usi(m) for m in board.legal_moves)
        if not legal:
            return ("resign", None)
        move = rng.choice(legal)
        board.push_usi(move)
        answers = sorted(cshogi.move_to_usi(m) for m in board.legal_moves)
        pv = (move, answers[0]) if answers else (move,)
        return (move, None, Score(cp=cp), pv)

    return reply


class CsaRig:
    """偽サーバ ＋ CsaTransport ＋ GameCore ＋ スクリプト式エンジン。"""

    def __init__(self, *, mode=Mode.AUTO, pov="mover", on_snapshot=None, keepalive=0, **server_kwargs):
        self.server = FakeCsaServer(FakeServerConfig(**server_kwargs))
        self.mode, self.pov, self.keepalive = mode, pov, keepalive
        self.log = EventLog(None, secrets=(SECRET,))
        self.hook = on_snapshot

    async def __aenter__(self):
        await self.server.start()
        self.engine = ScriptedEngine(color=Color.BLACK, emit=lambda e: self.core.post(e),
                                     auto_reply=random_reply())
        await self.engine.start()
        self.seat = LocalSeat(color=Color.BLACK, name="me", engine=self.engine, mode=self.mode)
        self.transport = CsaTransport(
            CsaSettings(host="127.0.0.1", port=self.server.port, login_name="me",
                        login_password=f"floodgate-300-10F,{SECRET}", comment_score_pov=self.pov,
                        keepalive_sec=self.keepalive),
            emit=lambda e: self.core.post(e), log=self.log)
        self.core = GameCore(my_seat=self.seat, transport=self.transport, policy=GamePolicy(),
                             log=self.log, strict=True,
                             on_snapshot=(lambda s: self.hook(self, s)) if self.hook else None)
        await self.transport.start()
        return self

    async def __aexit__(self, *exc):
        await self.transport.close()
        await self.server.close()

    async def play(self, timeout=20):
        await asyncio.wait_for(self.core.run(), timeout)

    def client_moves(self):
        return [l for l in self.server.received_lines if l[:1] in "+-"]


async def test_auto_game_to_the_end_with_comments():
    """M3 の完了条件＋シナリオ 13（mover 視点）＋ 22。"""
    async with CsaRig(opponent_resign_after=20, seed=3) as rig:
        await rig.play()
        assert rig.core.phase is Phase.ENDED
        assert (rig.core.result, rig.core.end_reason) == (GameResult.BLACK_WIN, EndReason.RESIGN)
        assert rig.server.login == ("me", f"floodgate-300-10F,{SECRET}", False)  # x1 なし
        assert [m.usi for m in rig.core.moves] == rig.server.moves and len(rig.server.moves) >= 20
        actors = {m.color: m.actor for m in rig.core.moves}
        assert actors == {Color.BLACK: Actor.ENGINE, Color.WHITE: Actor.REMOTE}
        sent = rig.client_moves()
        assert len(sent) == 11 and all(",'* 120 -" in line for line in sent)  # 評価値と相手の応手から始まる読み筋
        assert rig.server.deferred_moves == []  # 手番外に送っていない
        assert all(m.elapsed_ms == 0 for m in rig.core.moves)
        assert rig.engine.last_call("game_over").args["result"] == "win"
        # ログ: LOGIN のパスワードは伏せられ、秘密はどこにも出ない
        sends = [r["line"] for r in rig.log.records if r["kind"] == "csa_send"]
        assert sends[0] == "LOGIN me ***" and sends[1] == "AGREE"
        assert SECRET not in json.dumps(rig.log.records)


@pytest.mark.parametrize("pov, expected", [("mover", "120"), ("black", "-120")])
async def test_comment_sign_as_white(pov, expected):
    """シナリオ 13: 後手番で、指した側視点ならそのまま、先手視点なら反転して送る。"""
    async with CsaRig(client_color="-", pov=pov, opponent_resign_after=6,
                      opponent_comment="'* 55 -3334FU") as rig:
        await rig.play()
        assert rig.core.result is GameResult.WHITE_WIN
        sent = rig.client_moves()
        assert sent and all(line.split(",'* ")[1].split()[0] == expected for line in sent)
        # 相手（先手）のコメントは「指した側」視点に直して保持する。どちらの規約でも先手の値はそのまま
        remote = [m for m in rig.core.moves if m.actor is Actor.REMOTE]
        assert remote and all(m.score == Score(cp=55) for m in remote)
        assert rig.core.snapshot().my_color is Color.WHITE


def human_driver(moves_to_play):
    """スナップショットを見て、人間役として合法手を指す。規定数を指したら投了する。"""
    state = {"played": 0, "ply": -1}

    def on_snapshot(rig, s):
        if not s.legal_moves or s.ply == state["ply"]:
            return
        state["ply"] = s.ply
        if state["played"] < moves_to_play:
            state["played"] += 1
            rig.core.post(ev.MoveCommand(color=s.my_color, ply=s.ply, usi=sorted(s.legal_moves)[0]))
        else:
            rig.core.post(ev.ResignCommand(color=s.my_color, ply=s.ply))

    return on_snapshot


async def test_human_moves_carry_no_comment_to_server():
    """シナリオ 12: 人間の手にはサーバ向けコメントが付かない。手元の棋譜は mark_human_moves に従う。"""
    async with CsaRig(mode=Mode.MANUAL, on_snapshot=human_driver(4)) as rig:
        await rig.play()
        assert rig.core.end_reason is EndReason.RESIGN and rig.core.result is GameResult.WHITE_WIN
        sent = rig.client_moves()
        assert len(sent) == 4 and all("'" not in line and "," not in line for line in sent)
        assert "%TORYO" in rig.server.received_lines
        assert rig.engine.searches() == []  # エンジンは一度も考えていない
        assert rig.server.deferred_moves == []
        info = kifu.GameInfo(rig.core.game_id, "me", "opp", "2026-11-14T00:00:00")
        marked = kifu.render_csa(info, rig.core.moves, mark_human_moves=True)
        assert marked.count("'human") == 4
        assert "'human" not in kifu.render_csa(info, rig.core.moves, mark_human_moves=False)
        assert all(r.get("actor") == "human" for r in rig.log.records if r["kind"] == "commit")


async def test_takeover_mid_game_then_release():
    """CSA モードでの切替: エンジン → 人間 2 手 → エンジン。コメントはエンジンの手にだけ付く。"""
    state = {"human": 0, "ply": -1, "taken": False}

    def on_snapshot(rig, s):
        if s.phase is not Phase.PLAYING or s.my_color is None:
            return
        if s.ply == 4 and not state["taken"]:
            state["taken"] = True
            rig.core.post(ev.SetModeCommand(color=s.my_color, mode=Mode.MANUAL))
        if s.legal_moves and s.ply != state["ply"]:
            state["ply"] = s.ply
            state["human"] += 1
            rig.core.post(ev.MoveCommand(color=s.my_color, ply=s.ply, usi=sorted(s.legal_moves)[0]))
            if state["human"] == 2:
                rig.core.post(ev.SetModeCommand(color=s.my_color, mode=Mode.AUTO))

    async with CsaRig(on_snapshot=on_snapshot, opponent_resign_after=16) as rig:
        await rig.play()
        mine = [m for m in rig.core.moves if m.color is Color.BLACK]
        assert [m.actor for m in mine].count(Actor.HUMAN) == 2
        sent = rig.client_moves()
        assert len(sent) == len(mine)
        for line, record in zip(sent, mine):
            assert ("'*" in line) == (record.actor is Actor.ENGINE)
        assert rig.server.deferred_moves == [] and rig.core.result is GameResult.BLACK_WIN


async def test_remote_seat_cannot_be_taken():
    async with CsaRig(opponent_resign_after=4) as rig:
        rig.core.post(ev.SetModeCommand(color=Color.WHITE, mode=Mode.MANUAL))
        await rig.play()
        assert any("相手の席" in n for n in rig.core.notices)


# ---- CsaTransport 単体 ---------------------------------------------------- #


class Bare:
    def __init__(self, **server_kwargs):
        self.server = FakeCsaServer(FakeServerConfig(**server_kwargs))
        self.events: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        await self.server.start()
        self.transport = CsaTransport(
            CsaSettings(host="127.0.0.1", port=self.server.port, login_name="me", login_password="pw",
                        keepalive_sec=self.keepalive if hasattr(self, "keepalive") else 0),
            emit=self.events.put_nowait)
        await self.transport.start()
        return self

    async def __aexit__(self, *exc):
        await self.transport.close()
        await self.server.close()

    async def next(self, kind):
        while True:
            event = await asyncio.wait_for(self.events.get(), 10)
            if isinstance(event, kind):
                return event


async def test_transport_refuses_to_send_out_of_turn_or_twice():
    async with Bare(opponent_delay_ms=300, opponent_moves=["3c3d"]) as b:
        offered = await b.next(ev.GameOffered)
        assert offered.my_color is Color.BLACK and offered.time_control.increment_ms == 10_000
        with pytest.raises(TransportError):
            await b.transport.send_move(Color.BLACK, "7g7f")  # START 前
        await b.transport.agree()
        await b.next(ev.GameStarted)
        with pytest.raises(TransportError):
            await b.transport.send_move(Color.WHITE, "3c3d")  # 自分の席ではない
        await b.transport.send_move(Color.BLACK, "7g7f")
        with pytest.raises(TransportError):
            await b.transport.send_move(Color.BLACK, "2g2f")  # エコー待ち
        echo = await b.next(ev.MoveConfirmed)
        assert (echo.color, echo.usi, echo.elapsed_ms) == (Color.BLACK, "7g7f", 0)
        with pytest.raises(TransportError):
            await b.transport.send_move(Color.BLACK, "2g2f")  # 相手の手番
        with pytest.raises(TransportError):
            await b.transport.send_resign(Color.BLACK)
        reply = await b.next(ev.MoveConfirmed)
        assert (reply.color, reply.usi) == (Color.WHITE, "3c3d")
        await b.transport.send_resign(Color.BLACK)
        ended = await b.next(ev.GameEnded)
        assert (ended.result, ended.reason) == (GameResult.WHITE_WIN, EndReason.RESIGN)
        assert b.server.deferred_moves == []
        assert [l for l in b.server.received_lines if l[:1] == "+"] == ["+7776FU"]


async def test_login_failure_and_reject_and_disconnect():
    async with Bare(accept_login=False) as b:
        closed = await b.next(ev.TransportClosed)
        assert "LOGIN" in closed.detail
    async with Bare(reject_game=True) as b:
        await b.next(ev.GameOffered)
        await b.transport.agree()
        rejected = await b.next(ev.GameRejected)
        assert rejected.by == "fake_opponent"
    async with Bare() as b:
        await b.next(ev.GameOffered)
        await b.server.close()  # 対局前にサーバが落ちる
        await b.next(ev.TransportClosed)


async def test_connect_failure_is_reported():
    events = []
    transport = CsaTransport(CsaSettings(host="127.0.0.1", port=1, login_name="me", login_password="pw",
                                         connect_timeout_ms=2000), emit=events.append)
    await transport.start()
    assert isinstance(events[0], ev.TransportClosed)
    await transport.close()


async def test_keepalive_sends_blank_line_and_ignores_reply():
    rig = Bare()
    rig.keepalive = 0.05
    async with rig as b:
        await b.next(ev.GameOffered)
        for _ in range(200):  # 条件が成り立つまで待つ（上限つき）
            if "" in b.server.received_lines:
                break
            await asyncio.sleep(0.01)
        assert "" in b.server.received_lines
        await b.transport.agree()
        await b.next(ev.GameStarted)  # 空行の応答で壊れていない


# ---- アプリ全体（偽エンジンの子プロセス ＋ 偽サーバ） --------------------- #


async def test_run_csa_end_to_end_with_account_selection(tmp_path):
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=12))
    await server.start()
    try:
        args = ", ".join(json.dumps(a) for a in ("-m", "takeover.fakes.fake_engine", "--seed", "4", "--ponder"))
        (tmp_path / "config.toml").write_text(
            f'[engine.main]\npath = {json.dumps(sys.executable)}\nargs = [{args}]\nponder = true\n'
            f'[csa]\nhost = "127.0.0.1"\nport = {server.port}\nengine = "main"\ndefault_account = "a1"\n'
            '[policy]\nquit_timeout_ms = 2000\n', encoding="utf-8")
        (tmp_path / "accounts.toml").write_text(
            '[[account]]\nalias = "a1"\nname = "first"\npassword = "pw1"\n'
            f'[[account]]\nalias = "a2"\nname = "second_human"\ngame = "floodgate-300-10F"\ntrip = "{SECRET}"\n',
            encoding="utf-8")
        out = io.StringIO()
        code = await asyncio.wait_for(
            app.run_csa(tmp_path / "config.toml", account_alias="a2", headless=True,
                        stdin=open(os.devnull), out=out), 30)
        assert code == 0, out.getvalue()
        assert server.login == ("second_human", f"floodgate-300-10F,{SECRET}", False)
        assert "[終局] black_win / resign" in out.getvalue()
        (record,) = (tmp_path / "records").glob("*.csa")
        assert "fake-game-1" in record.name and "N+second_human" in record.read_text(encoding="utf-8")
        (log,) = (tmp_path / "logs").glob("*.jsonl")
        assert SECRET not in log.read_text(encoding="utf-8")
        assert "LOGOUT" in server.received_lines and server.deferred_moves == []
    finally:
        await server.close()


async def test_run_csa_reports_bad_account_and_login_failure(tmp_path):
    server = FakeCsaServer(FakeServerConfig(accept_login=False))
    await server.start()
    try:
        args = ", ".join(json.dumps(a) for a in ("-m", "takeover.fakes.fake_engine"))
        (tmp_path / "config.toml").write_text(
            f'[engine.main]\npath = {json.dumps(sys.executable)}\nargs = [{args}]\n'
            f'[csa]\nhost = "127.0.0.1"\nport = {server.port}\nengine = "main"\n', encoding="utf-8")
        (tmp_path / "accounts.toml").write_text(
            '[[account]]\nalias = "a1"\nname = "first"\npassword = "pw1"\n', encoding="utf-8")
        out = io.StringIO()
        assert await app.run_csa(tmp_path / "config.toml", account_alias="zzz", headless=True, out=out) == 1
        assert "a1" in out.getvalue()
        out = io.StringIO()
        code = await asyncio.wait_for(
            app.run_csa(tmp_path / "config.toml", account_alias="a1", headless=True,
                        stdin=open(os.devnull), out=out), 30)
        assert code == 1 and "LOGIN" in out.getvalue()
    finally:
        await server.close()
