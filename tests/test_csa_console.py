"""CSA の通信欄（SPEC §6.11）: 通信ログ、サーバへ送る 1 行の振り分け、指し手へのコメント、拡張モード（x1）。"""

import asyncio
import io
import json

import pytest

from test_csa_transport import SECRET, CsaRig, random_reply
from test_session import Ui, free_port, until, write_config
from takeover import app
from takeover.core import events as ev
from takeover.core.game import GameCore, GamePolicy
from takeover.core.seat import LocalSeat
from takeover.core.types import Actor, Color, EndReason, GameResult, Mode, Phase
from takeover.fakes.fake_csa_server import FakeCsaServer, FakeServerConfig
from takeover.fakes.scripted_engine import ScriptedEngine
from takeover.record import kifu
from takeover.record.eventlog import EventLog
from takeover.transport import csa_parse as c
from takeover.transport.csa import CsaSettings, CsaTransport, TransportError
from takeover.ui import messages as m


# ---- 純関数 ---------------------------------------------------------------- #


def test_classify_raw_line():
    kinds = {
        "": "empty", "LOGIN a b": "login", "login a b x1": "login", "LOGOUT": "logout",
        "AGREE": "auto", "REJECT": "auto", "+7776FU": "move", "-3334FU,'* 1": "move",
        "%TORYO": "resign", "%toryo": "resign", "%KACHI": "declare", "%HIKIWAKE": "special", "%CHUDAN": "special",
        "'*comment": "comment", "'hello": "comment",
        "%%WHO": "extended", "%%GAME test-300-10 *": "extended", "%%CHALLENGE test-300-10 +": "extended",
        "hello": "other",
    }
    for line, kind in kinds.items():
        assert c.classify_raw_line(line) == kind, line


def test_clean_line_and_comment():
    assert c.clean_line("  %%WHO\r\n") == "%%WHO"
    assert c.clean_line("%%CHAT a\nLOGOUT") == "%%CHAT aLOGOUT"  # 改行で 2 行に分けて送らせない
    assert len(c.clean_line("x" * 1000)) == c.MAX_RAW_LINE
    # コメントは「サーバの棋譜に残したい形」で持つ
    assert c.clean_comment("'*良い手, たぶん") == "'*良い手、 たぶん"  # , はサーバが区切りに使う
    assert c.clean_comment("just text") == "'* just text"
    assert c.clean_comment("'memo") == "'*memo"
    assert c.clean_comment("'** 31511 -5251OU") == "'** 31511 -5251OU"
    assert c.clean_comment("  ") == "" and c.clean_comment("'*") == "" and c.clean_comment("'") == ""
    assert c.clean_comment("'* 一行目\r\n\n二行目\n'") == "'* 一行目\n'* 二行目"  # 複数行
    assert len(c.clean_comment("\n".join(f"'* {i}" for i in range(30))).split("\n")) == c.MAX_COMMENT_LINES
    # サーバは `'X` を棋譜に `'*X` と書く（* を 1 つ足す）ので、1 つ目は * を 1 つ減らして送る
    assert c.comment_wire_segments("'** 31511 -5251OU") == ["'* 31511 -5251OU"]
    assert c.comment_wire_segments("'* メモ\n'* 続き") == ["' メモ", "'* 続き"]
    assert c.comment_wire_segments("") == []
    assert c.build_move_line("+7776FU", comment="'** 30 -3334FU") == "+7776FU,'* 30 -3334FU"
    assert c.build_move_line("+7776FU", comment="'* a\n'* b") == "+7776FU,' a,'* b"
    assert c.build_move_line("+7776FU", score=30, pv_csa=("-3334FU",), comment="'* hi") == "+7776FU,' hi"  # 自由コメントが優先


def test_console_messages_parse():
    def parse(data):
        return m.parse_client_message(json.dumps(data), client_id="c1")

    assert parse({"type": "raw_csa", "line": " %%WHO\n"}) == ev.SendRawCommand(line="%%WHO", client_id="c1")
    assert parse({"type": "set_extended", "enabled": True}) == ev.SetExtendedCommand(enabled=True, client_id="c1")
    move = parse({"type": "move", "color": "b", "ply": 0, "usi": "7g7f", "comment": "'*a,b"})
    assert move == ev.MoveCommand(color=Color.BLACK, ply=0, usi="7g7f", comment="'*a、b", client_id="c1")
    assert parse({"type": "move", "color": "b", "ply": 0, "usi": "7g7f"}).comment == ""
    for bad in ({"type": "raw_csa"}, {"type": "raw_csa", "line": 3}, {"type": "raw_csa", "line": "x" * 500},
                {"type": "set_extended", "enabled": 1}, {"type": "move", "color": "b", "ply": 0, "usi": "7g7f", "comment": 5}):
        assert parse(bad) is None
    assert m.csa_log_message([{"dir": "send", "line": "x", "time": "t"}], reset=True) == {
        "type": "csa_log", "reset": True, "lines": [{"dir": "send", "line": "x", "time": "t"}]}


# ---- CsaTransport と GameCore --------------------------------------------- #


def commenting_human(comments):
    """人間役: comments の順にコメントを付けて指し、尽きたら投了する。"""
    state = {"ply": -1, "left": list(comments)}

    def on_snapshot(rig, s):
        if not s.legal_moves or s.ply == state["ply"]:
            return
        state["ply"] = s.ply
        if state["left"]:
            rig.core.post(ev.MoveCommand(color=s.my_color, ply=s.ply, usi=sorted(s.legal_moves)[0],
                                         comment=state["left"].pop(0)))
        else:
            rig.core.post(ev.ResignCommand(color=s.my_color, ply=s.ply))

    return on_snapshot


async def test_human_move_can_carry_a_free_comment():
    """入力した形（残したい形）がサーバの棋譜にそのまま残る。Floodgate での確認（2026-11-17）:
    `'** 31511 -5251OU` をそのまま送ると、サーバが * を 1 つ足して `'*** …` になっていた。"""
    comments = ["'** 31511 -5251OU", "", "'* 勘で指した\n'* 二行目"]
    async with CsaRig(mode=Mode.MANUAL, on_snapshot=commenting_human(comments)) as rig:
        await rig.play()
        sent = rig.client_moves()
        assert len(sent) == 3
        assert sent[0].endswith(",'* 31511 -5251OU") and "," not in sent[1]
        assert sent[2].endswith(",' 勘で指した,'* 二行目")
        # 偽サーバは本物と同じ規則で棋譜のコメントを作る（1 つ目の区切りだけ、* を 1 つ足して書く）
        assert rig.server.kifu_comments == ["'** 31511 -5251OU", "'* 勘で指した"]
        assert [r.comment for r in rig.core.moves if r.actor is Actor.HUMAN] == comments
        info = kifu.GameInfo(rig.core.game_id, "me", "opp", "2026-11-14T00:00:00")
        text = kifu.render_csa(info, rig.core.moves, mark_human_moves=True)
        assert "'** 31511 -5251OU\n'human" in text and "'* 勘で指した\n'* 二行目\n'human" in text  # 手元は全行
        kif = kifu.render_kif(info, rig.core.moves, mark_human_moves=False)
        assert "*31511 -5251OU" in kif and "*勘で指した\n*二行目" in kif
        assert rig.server.deferred_moves == []


async def test_extended_commands_do_not_disturb_the_game_and_lines_are_observable():
    lines = []
    sent_who = []

    def hook(rig, s):  # 相手の手番でも自分の手番でも、拡張コマンドは送ってよい
        if s.phase is Phase.PLAYING and s.ply in (1, 2) and s.ply not in sent_who:
            sent_who.append(s.ply)
            asyncio.get_running_loop().create_task(rig.transport.send_raw("%%WHO"))

    async with CsaRig(on_snapshot=hook, opponent_resign_after=8) as rig:
        rig.transport._on_line_hook = lambda direction, line: lines.append((direction, line))
        await rig.play()
        assert (rig.core.result, rig.core.end_reason) == (GameResult.BLACK_WIN, EndReason.RESIGN)
        assert rig.server.received_lines.count("%%WHO") == 2 and rig.server.deferred_moves == []
        assert ("send", "%%WHO") in lines and ("recv", "##[WHO] +OK") in lines
        assert ("recv", "#RESIGN") in lines and any(d == "send" and l.startswith("+") for d, l in lines)
        assert not any(SECRET in line for _, line in lines)


async def test_send_raw_refuses_game_lines():
    async with CsaRig(mode=Mode.MANUAL, opponent_delay_ms=50) as rig:
        await rig.core.queue.get() and None
        for line in ("+7776FU", "-3334FU", "%TORYO", "%KACHI", "%HIKIWAKE", "LOGIN a b", "LOGOUT", "AGREE", "'*x", ""):
            with pytest.raises(TransportError):
                await rig.transport.send_raw(line)
        with pytest.raises(TransportError):
            await rig.transport.send_special(Color.BLACK, "%%WHO")  # %X ではない
        await asyncio.sleep(0.1)
        assert [l for l in rig.server.received_lines if not l.startswith("LOGIN")] == []


async def test_raw_special_goes_through_turn_checks_and_ends_as_illegal():
    """%HIKIWAKE など未知の %X は、MANUAL の自手番にだけ送る。shogi-server はそれを非合法手として扱う。"""
    state = {"tried": False}

    def hook(rig, s):
        if s.phase is not Phase.PLAYING or state["tried"]:
            return
        if s.ply == 1:  # 相手の手番: 送らない
            state["tried"] = True
            rig.core.post(ev.RawSpecialCommand(color=Color.BLACK, ply=s.ply, text="%HIKIWAKE"))

    async with CsaRig(on_snapshot=hook, opponent_delay_ms=100, opponent_resign_after=6) as rig:
        await rig.play()
        assert "%HIKIWAKE" not in rig.server.received_lines and rig.server.deferred_moves == []
        assert any("手番ではありません" in n for n in rig.core.notices)

    def manual_hook(rig, s):
        if s.legal_moves and s.ply == 0 and not state.get("sent"):
            state["sent"] = True
            rig.core.post(ev.RawSpecialCommand(color=Color.BLACK, ply=0, text="%HIKIWAKE"))
            rig.core.post(ev.MoveCommand(color=Color.BLACK, ply=0, usi="7g7f"))  # 同じ手番の 2 つ目は拒否（I7）

    async with CsaRig(mode=Mode.MANUAL, on_snapshot=manual_hook) as rig:
        await rig.play()
        assert rig.server.received_lines.count("%HIKIWAKE") == 1 and rig.client_moves() == []
        assert (rig.core.result, rig.core.end_reason) == (GameResult.WHITE_WIN, EndReason.ILLEGAL_MOVE)


async def test_extended_mode_login_waits_for_game_request():
    """x1: LOGIN だけでは対局にならない。%%GAME を送ると対局が始まる。`##` の応答は終局理由として扱わない。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6))
    await server.start()
    log = EventLog(None)
    holder = {}
    engine = ScriptedEngine(color=Color.BLACK, emit=lambda e: holder["core"].post(e), auto_reply=random_reply())
    await engine.start()
    transport = CsaTransport(
        CsaSettings(host="127.0.0.1", port=server.port, login_name="me", login_password="pw", x1=True, keepalive_sec=0),
        emit=lambda e: holder["core"].post(e), log=log)
    core = GameCore(my_seat=LocalSeat(color=Color.BLACK, name="me", engine=engine, mode=Mode.AUTO),
                    transport=transport, policy=GamePolicy(), log=log, strict=True)
    holder["core"] = core
    try:
        await transport.start()
        task = asyncio.create_task(core.run())
        await until(lambda: core.phase is Phase.WAITING_GAME)
        await asyncio.sleep(0.2)
        assert server.login == ("me", "pw", True) and core.game_id is None  # まだ対局は決まらない
        await transport.send_raw("%%WHO")
        await transport.send_raw("%%GAME testgame-300-10F *")
        await asyncio.wait_for(task, 20)
        assert server.game_requests == ["%%GAME testgame-300-10F *"]
        assert (core.result, core.end_reason) == (GameResult.BLACK_WIN, EndReason.RESIGN)
    finally:
        await transport.close()
        await server.close()


# ---- アプリ全体（UI の通信欄） -------------------------------------------- #


async def test_console_log_and_raw_commands_from_ui(tmp_path):
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=30, opponent_delay_ms=150, match_delay_sec=1))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port)
    task = asyncio.create_task(app.run_csa(config, account_alias="a2", out=io.StringIO()))
    logs = []
    try:
        async with Ui(ui_port) as ui:
            def log_lines():
                out, seen = [], []
                for raw in ui.raw:
                    msg = json.loads(raw)
                    if msg["type"] == "csa_log":
                        if msg["reset"]:
                            out = []
                        out += [(x["dir"], x["line"]) for x in msg["lines"]]
                return out

            await ui.state("waiting")
            await ui.send(type="raw_csa", line="%%WHO")  # 対局待ちの間でも送れる
            await until(lambda: ("recv", "##[WHO] +OK") in log_lines())
            assert ("send", "LOGIN second_human ***") in log_lines()  # LOGIN は伏せて表示する
            await ui.state("playing")
            await until(lambda: ui.snapshot is not None and ui.snapshot["ply"] >= 2)
            for line in ("+7776FU", "LOGIN x y", "AGREE", "'*hello", "%HIKIWAKE", "%KACHI"):
                await ui.send(type="raw_csa", line=line)
            await ui.send(type="raw_csa", line="%%LIST")
            await until(lambda: ("recv", "##[LIST] +OK") in log_lines())
            await until(lambda: sum("送れません" in n or "自動で送ります" in n for n in ui.notices) >= 4)
            # エンジンが指している（AUTO）ので、%HIKIWAKE / %KACHI は GameCore が拒否する
            await until(lambda: sum("AUTO の席です" in n or "手番ではありません" in n for n in ui.notices) >= 2)
            got = [l for l in server.received_lines]
            assert "%HIKIWAKE" not in got and "%KACHI" not in got and "'*hello" not in got
            assert not any(l.startswith("LOGIN x") for l in got) and "AGREE" in got and got.count("AGREE") == 1
            assert server.deferred_moves == []  # 手番外の指し手を 1 つも送っていない
            assert ui.session["extended"] is False

            # 後から繋いだ画面にも、それまでの通信ログが届く
            async with Ui(ui_port) as second:
                await until(lambda: any(json.loads(r)["type"] == "csa_log" and json.loads(r)["reset"] for r in second.raw))
                first = next(json.loads(r) for r in second.raw if json.loads(r)["type"] == "csa_log")
                assert any(x["line"] == "LOGIN second_human ***" for x in first["lines"])
                logs.extend(second.raw)
            logs.extend(ui.raw)
        assert SECRET not in "\n".join(logs)
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()


async def test_extended_mode_requests_the_account_game_by_default(tmp_path):
    """x1 でも、名前ストックのゲーム名で既定の %%GAME を送って対局待ちに入る。パスワード欄は識別文字列だけ
    （通常モードでサーバが取り出すパスワードと同じ＝同じプレイヤーとして扱われる）。"""
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port, extra_csa="extended_mode = true\n")
    task = asyncio.create_task(app.run_csa(config, account_alias="a2", out=io.StringIO()))
    logs = []
    try:
        async with Ui(ui_port) as ui:
            await until(lambda: ui.snapshot is not None and ui.snapshot["phase"] == "ended")
            assert ui.session["extended"] is True
            assert server.logins == [("second_human", SECRET, True)]
            assert server.game_requests == ["%%GAME floodgate-300-10F *"]
            logs.extend(ui.raw)
        assert SECRET not in "\n".join(logs)  # x1 の LOGIN 行も伏せて表示する
        assert len(list((tmp_path / "records").glob("*.csa"))) == 1
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()


async def test_extended_mode_from_ui_then_game_request(tmp_path):
    server = FakeCsaServer(FakeServerConfig(opponent_resign_after=6))
    await server.start()
    ui_port = free_port()
    config = write_config(tmp_path, server.port, ui_port=ui_port)
    task = asyncio.create_task(app.run_csa(config, account_alias=None, out=io.StringIO()))
    try:
        async with Ui(ui_port) as ui:
            await ui.state("lobby")
            await ui.send(type="raw_csa", line="%%WHO")
            await until(lambda: any("接続していない" in n for n in ui.notices))
            await ui.send(type="set_extended", enabled=True)
            await until(lambda: ui.session["extended"] is True)
            await ui.send(type="connect", alias="a1")
            await ui.state("waiting")
            await asyncio.sleep(0.3)
            # a1 はゲーム名のない名前（password だけ）なので、既定の %%GAME は送らない
            assert server.logins == [("first", "pw1", True)] and ui.session["state"] == "waiting"
            assert server.game_requests == []
            await ui.send(type="raw_csa", line="%%GAME testgame-300-10F *")
            await until(lambda: ui.snapshot is not None and ui.snapshot["phase"] == "ended")
            assert server.game_requests == ["%%GAME testgame-300-10F *"]
            assert len(list((tmp_path / "records").glob("*.csa"))) == 1
    finally:
        task.cancel()
        assert await asyncio.wait_for(task, 20) in (0, 1, 130)
        await server.close()
