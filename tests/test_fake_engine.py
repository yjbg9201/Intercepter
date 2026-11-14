"""偽エンジン（子プロセス）が USI の基本に応答すること（M0 の完了条件）。"""

import asyncio
import subprocess

import cshogi

from conftest import fake_engine_argv


class Proc:
    def __init__(self, proc):
        self.proc = proc

    @classmethod
    async def spawn(cls, *args):
        proc = await asyncio.create_subprocess_exec(
            *fake_engine_argv(*args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return cls(proc)

    def send(self, line):
        self.proc.stdin.write((line + "\n").encode())

    async def read_raw(self):
        return await asyncio.wait_for(self.proc.stdout.readline(), 10)

    async def until(self, prefix):
        lines = []
        while True:
            raw = await self.read_raw()
            assert raw, f"EOF before {prefix!r}: {lines}"
            line = raw.decode().rstrip("\r\n")
            lines.append(line)
            if line.startswith(prefix):
                return lines

    async def quit(self):
        self.send("quit")
        assert await asyncio.wait_for(self.proc.wait(), 10) == 0


async def test_handshake_go_quit():
    e = await Proc.spawn("--seed", "1")
    e.send("usi")
    lines = await e.until("usiok")
    assert any(l.startswith("id name") for l in lines)
    e.send("isready")
    await e.until("readyok")
    e.send("usinewgame")
    e.send("position startpos moves 7g7f")
    e.send("go btime 1000 wtime 1000 byoyomi 0")
    best = (await e.until("bestmove"))[-1].split()[1]
    board = cshogi.Board()
    board.push_usi("7g7f")
    assert board.is_legal(board.move_from_usi(best))
    await e.quit()


async def test_deterministic_for_same_seed():
    moves = []
    for _ in range(2):
        e = await Proc.spawn("--seed", "7", "--ponder")
        e.send("position startpos")
        e.send("go")
        moves.append((await e.until("bestmove"))[-1])
        await e.quit()
    assert moves[0] == moves[1] and " ponder " in moves[0]


async def test_stop_during_long_think_and_ponderhit():
    e = await Proc.spawn("--think-ms", "60000")
    e.send("position startpos")
    e.send("go btime 0 wtime 0")
    e.send("stop")
    assert (await e.until("bestmove"))[-1].startswith("bestmove")
    e.send("go ponder btime 0 wtime 0")
    e.send("stop")
    await e.until("bestmove")
    await e.quit()

    e = await Proc.spawn("--think-ms", "0")
    e.send("position startpos")
    e.send("go ponder")
    e.send("ponderhit")
    await e.until("bestmove")
    await e.quit()


async def test_crlf_and_resign_and_eof():
    e = await Proc.spawn("--crlf", "--resign")
    e.send("usi")
    raw = await e.read_raw()
    assert raw.endswith(b"\r\n")
    await e.until("usiok")
    e.send("position startpos")
    e.send("go")
    assert (await e.until("bestmove"))[-1] == "bestmove resign"
    e.proc.stdin.close()  # EOF で終了する
    assert await asyncio.wait_for(e.proc.wait(), 10) == 0


async def test_crash_on_go_and_illegal_move():
    e = await Proc.spawn("--crash-on-go", "1")
    e.send("position startpos")
    e.send("go")
    assert await asyncio.wait_for(e.proc.wait(), 10) == 3

    e = await Proc.spawn("--illegal-move")
    e.send("position startpos")
    e.send("go")
    best = (await e.until("bestmove"))[-1].split()[1]
    board = cshogi.Board()
    move = board.move_from_usi(best)
    assert not (move and board.is_legal(move))
    await e.quit()
