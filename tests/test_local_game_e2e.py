"""ヘッドレスで偽エンジン（子プロセス）同士が終局まで指せる（M2 の完了条件）。"""

import asyncio
import io
import json
import os
import subprocess
import sys

from takeover import app
from takeover.core import events as ev
from takeover.core.types import Color, Mode
from rig import Rig


def write_config(tmp_path, *, black_args=(), white_args=(), extra="", local_extra=""):
    def engine(name, args):
        arglist = ", ".join(json.dumps(a) for a in ("-m", "takeover.fakes.fake_engine", *args))
        return f'[engine.{name}]\npath = {json.dumps(sys.executable)}\nargs = [{arglist}]\nponder = true\n'

    text = (
        engine("b", ("--seed", "1", "--ponder", *black_args))
        + engine("w", ("--seed", "2", "--ponder", *white_args))
        + '[local]\nblack = "b"\nwhite = "w"\ntotal_sec = 300\nincrement_sec = 30\nmax_moves = 256\n'
        + local_extra
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


def test_stdin_line_splitter():
    buf = bytearray()
    assert app.split_stdin_lines(buf, b"take b\nrelease b\r\nmo") == ["take b", "release b"]
    assert app.split_stdin_lines(buf, b"ve 7g7f\n") == ["move 7g7f"]
    assert app.split_stdin_lines(buf, b"") == [] and not buf


class _TriggerOut(io.StringIO):
    """出力に needle が現れたら 1 回だけ action を呼ぶ（sleep に頼らず、対局の進行に合わせて操作するため）。"""

    def __init__(self, needle, action):
        super().__init__()
        self._needle, self._action, self._fired = needle, action, False

    def write(self, s):
        n = super().write(s)
        if not self._fired and self._needle in self.getvalue():
            self._fired = True
            self._action()
        return n


async def test_stdin_lines_arriving_together_are_all_processed(tmp_path):
    """実エンジンでの確認（2026-11-16）で見つけた不具合: 同時に届いた複数行のうち 1 行目しか処理されなかった。"""
    import asyncio

    config = write_config(tmp_path, white_args=("--think-ms", "5000"))
    rfd, wfd = os.pipe()
    out = _TriggerOut("  1 ▲", lambda: os.write(wfd, b"take b\ntake w\nresign w\n"))
    with os.fdopen(rfd, "rb", buffering=0) as stdin:
        code = await asyncio.wait_for(app.run_local(config, headless=True, stdin=stdin, out=out), 20)
    os.close(wfd)
    text = out.getvalue()
    assert code == 0, text
    assert "先手 → MANUAL" in text and "後手 → MANUAL" in text
    assert "[終局] black_win / resign（1 手）" in text
    assert children() == []


async def test_signal_during_cleanup_does_not_skip_engine_quit(tmp_path):
    """片付け中（棋譜保存の後、エンジン終了の前）に SIGINT が来ても、quit → wait まで行う。"""
    import asyncio
    import signal

    config = write_config(tmp_path, white_args=("--resign-after", "4", "--ignore-quit", "--no-exit-on-eof"))
    out = _TriggerOut("棋譜を保存しました", lambda: os.kill(os.getpid(), signal.SIGINT))
    code = await asyncio.wait_for(app.run_local(config, headless=True, stdin=open(os.devnull), out=out), 30)
    assert code == 0, out.getvalue()
    (log,) = (tmp_path / "logs").glob("*.jsonl")
    assert sum('"engine_exit"' in l and '"killed": true' in l for l in log.read_text(encoding="utf-8").splitlines()) == 1  # 白だけ kill
    assert children() == []


# ---- 先後を入れ替えた連続対局（SPEC §6.6） -------------------------------- #


async def test_series_alternates_sides_and_reports_totals(tmp_path):
    """local.games 局を続けて指す。1 局ごとに先後を入れ替え、棋譜は 1 局ずつ保存し、通算成績を出す。"""
    before = set(children())
    # エンジン w は 6 手目以降の自分の手番で投了する → どちらの手番を持っても w が負ける
    config = write_config(tmp_path, white_args=("--resign-after", "6"),
                          local_extra='games = 3\nnext_game_delay_sec = 0\n')
    out = io.StringIO()
    code = await asyncio.wait_for(app.run_local(config, headless=True, stdin=open(os.devnull), out=out), 60)
    text = out.getvalue()
    assert code == 0, text
    records = sorted((tmp_path / "records").glob("*.csa"), key=lambda p: p.stat().st_mtime_ns)
    assert len(records) == 3
    names = [tuple(l[2:] for l in r.read_text(encoding="utf-8").splitlines() if l[:2] in ("N+", "N-"))
             for r in records]
    assert names == [("b", "w"), ("w", "b"), ("b", "w")]  # 先手・後手の名前が 1 局ごとに入れ替わる
    assert text.count("[終局]") == 3
    assert "第 2 局（全 3 局）: 先手 w / 後手 b" in text
    assert "[終局] black_win / resign" in text and "[終局] white_win / resign" in text
    assert "通算（3 局）: b 3 勝 / w 0 勝 / 引き分け 0" in text
    (log,) = (tmp_path / "logs").glob("*.jsonl")  # ログは連続対局で 1 本
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert sum(r["kind"] == "game_ended" for r in rows) == 3
    assert sum(r["kind"] == "engine_exit" for r in rows if "killed" in r) == 6  # 1 局ごとに両エンジンを終了している
    assert set(children()) <= before  # 子プロセスが残っていない


async def test_series_without_alternation_keeps_sides(tmp_path):
    config = write_config(tmp_path, white_args=("--resign-after", "6"),
                          local_extra='games = 2\nalternate_sides = false\nnext_game_delay_sec = 0\n')
    out = io.StringIO()
    code = await asyncio.wait_for(app.run_local(config, headless=True, stdin=open(os.devnull), out=out), 60)
    assert code == 0, out.getvalue()
    for record in (tmp_path / "records").glob("*.csa"):
        assert "N+b\nN-w\n" in record.read_text(encoding="utf-8")
    assert len(list((tmp_path / "records").glob("*.csa"))) == 2
    assert "通算（2 局）: b 2 勝 / w 0 勝 / 引き分け 0" in out.getvalue()


async def test_series_stops_when_an_engine_fails_to_start(tmp_path):
    """途中の局でエンジンが起動できなければ、そこで終わる（終了コード 1。残りの局は指さない）。"""
    flag = tmp_path / "started_once"
    script = tmp_path / "flaky.py"
    script.write_text(
        "import os, sys, runpy\n"
        f"flag = {str(flag)!r}\n"
        "if os.path.exists(flag):\n    sys.exit(3)\n"  # 2 回目の起動は即終了（usiok を返さない）
        "open(flag, 'w').close()\n"
        "sys.argv = ['fake_engine', '--seed', '1']\n"
        "runpy.run_module('takeover.fakes.fake_engine', run_name='__main__')\n", encoding="utf-8")
    config = write_config(tmp_path, white_args=("--resign-after", "4"), extra="usi_timeout_ms = 3000\n",
                          local_extra="games = 3\nnext_game_delay_sec = 0\n")
    text = config.read_text(encoding="utf-8")
    first_args = text[text.index("args = ["):text.index("\n", text.index("args = ["))]  # エンジン b の起動引数
    config.write_text(text.replace(first_args, f"args = [{json.dumps(str(script))}]", 1), encoding="utf-8")
    out = io.StringIO()
    code = await asyncio.wait_for(app.run_local(config, headless=True, stdin=open(os.devnull), out=out), 60)
    assert code == 1, out.getvalue()
    assert len(list((tmp_path / "records").glob("*.csa"))) == 1 and "エラー" in out.getvalue()
    assert children() == []


def test_series_config_validation(tmp_path):
    from takeover.config import ConfigError, parse_config
    import pytest
    from pathlib import Path

    base = {"engine": {"m": {"path": "/x"}}, "local": {"black": "m", "white": "m", "increment_sec": 30}}
    cfg = parse_config(base, base_dir=Path("."))
    assert (cfg.local.games, cfg.local.alternate_sides, cfg.local.next_game_delay_sec) == (1, True, 3)
    for bad in ({"games": 0}, {"games": -1}, {"next_game_delay_sec": -1}):
        with pytest.raises(ConfigError):
            parse_config({**base, "local": {**base["local"], **bad}}, base_dir=Path("."))
