"""ヘッドレスで偽エンジン（子プロセス）同士が終局まで指せる（M2 の完了条件）。"""

import io
import json
import os
import subprocess
import sys

from takeover import app
from takeover.core import events as ev
from takeover.core.types import Color, Mode
from rig import Rig


def write_config(tmp_path, *, black_args=(), white_args=(), extra=""):
    def engine(name, args):
        arglist = ", ".join(json.dumps(a) for a in ("-m", "takeover.fakes.fake_engine", *args))
        return f'[engine.{name}]\npath = {json.dumps(sys.executable)}\nargs = [{arglist}]\nponder = true\n'

    text = (
        engine("b", ("--seed", "1", "--ponder", *black_args))
        + engine("w", ("--seed", "2", "--ponder", *white_args))
        + '[local]\nblack = "b"\nwhite = "w"\ntotal_sec = 300\nincrement_sec = 30\nmax_moves = 256\n'
        + '[policy]\nstop_grace_ms = 2000\nquit_timeout_ms = 2000\n'
        + extra
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def children():
    out = subprocess.run(["pgrep", "-f", "takeover.fakes.fake_engine"], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


async def test_fake_engines_play_to_the_end_headless(tmp_path):
    before = set(children())
    config = write_config(tmp_path, white_args=("--resign-after", "40"))
    out = io.StringIO()
    code = await app.run_local(config, headless=True, stdin=open(os.devnull), out=out)
    text = out.getvalue()
    assert code == 0, text
    assert "[終局] black_win / resign" in text
    (record,) = (tmp_path / "records").glob("*.csa")
    body = record.read_text(encoding="utf-8")
    assert "%TORYO" in body and "'**" in body and "'human" not in body
    (log,) = (tmp_path / "logs").glob("*.jsonl")
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    kinds = {r["kind"] for r in rows}
    assert {"usi_send", "usi_recv", "commit", "ponder", "ponder_miss", "search_start", "game_ended"} <= kinds
    assert any(r["kind"] == "usi_send" and r["line"].startswith("go ponder") for r in rows)
    assert set(children()) <= before  # 子プロセスが残っていない


async def test_engine_start_failure_is_reported(tmp_path):
    config = write_config(tmp_path, black_args=("--no-usiok",), extra="usi_timeout_ms = 300\n")
    out = io.StringIO()
    assert await app.run_local(config, headless=True, stdin=open(os.devnull), out=out) == 1
    assert "usiok" in out.getvalue() and children() == []


def test_config_error_is_reported(tmp_path):
    import asyncio

    out = io.StringIO()
    assert asyncio.run(app.run_local(tmp_path / "missing.toml", headless=True, out=out)) == 1
    assert "設定エラー" in out.getvalue()


async def test_stdin_commands(fake_clock):
    rig = await Rig(fake_clock).start()
    snap = rig.snap
    parse = app.parse_stdin_command
    assert parse("take b", snap) == ev.SetModeCommand(color=Color.BLACK, mode=Mode.MANUAL, client_id="stdin")
    assert parse("release w", snap) == ev.SetModeCommand(color=Color.WHITE, mode=Mode.AUTO, client_id="stdin")
    assert parse("move 7g7f", snap) == ev.MoveCommand(color=Color.BLACK, ply=0, usi="7g7f", client_id="stdin")
    assert parse("declare b", snap) == ev.DeclareCommand(color=Color.BLACK, ply=0, client_id="stdin")
    assert parse("resign", snap) == ev.ResignCommand(color=Color.BLACK, ply=0, client_id="stdin")
    assert parse("hint on", snap) == ev.SetHintCommand(enabled=True, client_id="stdin")
    assert parse("", snap) is None
    assert isinstance(parse("take x", snap), str) and isinstance(parse("dance", snap), str)
    # 実際に GameCore へ流す
    for line in ("take b", "move 7g7f"):
        rig.core.post(parse(line, rig.snap))
        await rig.pump()
    assert rig.usi() == ["7g7f"] and rig.actors() == ["human"]
