"""偽 USI エンジン（子プロセス）。`python -m takeover.fakes.fake_engine [引数]` で起動する。

参照: SPEC §9「偽エンジン」

- cshogi でランダムな合法手を返す。合法手がなければ `bestmove resign`。
- 標準入力を 1 行ずつ読み、標準出力へ返す。ここは別プロセスなので I3 の対象外
  （思考中に stop を受け取るために読み取りスレッドを使う）。
- 同じ seed・同じ局面なら同じ手を返す（決定的）。
- 故障・挙動の切り替えは `--help` を参照。
"""

from __future__ import annotations

import argparse
import queue
import random
import sys
import threading
import time

import cshogi


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fake_engine", description="テスト用の偽 USI エンジン")
    p.add_argument("--name", default="FakeEngine")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--think-ms", type=int, default=0, help="go から bestmove までの時間")
    p.add_argument("--ponder", action="store_true", help="bestmove に ponder 手を付ける")
    p.add_argument("--multipv", type=int, default=1, help="MultiPV の初期値（setoption でも変わる）")
    p.add_argument("--stop-delay-ms", type=int, default=0, help="stop から bestmove までの遅延")
    p.add_argument("--ignore-stop", action="store_true", help="stop を無視する（ハング）")
    p.add_argument("--crash-rate", type=float, default=0.0, help="go のたびにこの確率で異常終了")
    p.add_argument("--crash-on-go", type=int, default=0, help="N 回目の go で異常終了（0 は無効）")
    p.add_argument("--crlf", action="store_true", help="行末を CRLF にする")
    p.add_argument("--illegal-move", action="store_true", help="非合法手を返す")
    p.add_argument("--resign", action="store_true", help="常に bestmove resign")
    p.add_argument("--resign-after", type=int, default=0, help="この手数以降は bestmove resign（0 は無効）")
    p.add_argument("--win", action="store_true", help="常に bestmove win")
    p.add_argument("--rogue-ponder-bestmove", action="store_true",
                   help="ponder 中に stop / ponderhit を待たず bestmove を返す（USI 違反）")
    p.add_argument("--no-usiok", action="store_true")
    p.add_argument("--no-readyok", action="store_true")
    p.add_argument("--startup-ms", type=int, default=0, help="isready から readyok までの時間")
    p.add_argument("--huge-info", type=int, default=0, help="bestmove の前にこのバイト数の info 行を出す")
    p.add_argument("--exit-on-eof", action=argparse.BooleanOptionalAction, default=True)
    return p


class FakeEngine:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.eol = "\r\n" if args.crlf else "\n"
        self.board = cshogi.Board()
        self.multipv = max(1, args.multipv)
        self.go_count = 0
        self.crash_rng = random.Random(args.seed ^ 0x5EED)
        self.inbox: queue.Queue[str | None] = queue.Queue()

    # ---- I/O ----

    def send(self, line: str) -> None:
        sys.stdout.write(line + self.eol)
        sys.stdout.flush()

    def reader(self) -> None:
        for raw in sys.stdin:
            self.inbox.put(raw.rstrip("\r\n"))
        self.inbox.put(None)

    def recv(self, timeout: float | None) -> str | None | bool:
        """次の行。EOF は None、タイムアウトは False。"""
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return False

    # ---- 指し手選び ----

    def rng(self) -> random.Random:
        return random.Random(f"{self.args.seed}:{self.board.sfen()}")

    def pick(self) -> tuple[str, str | None, list[str]]:
        """(bestmove, ponder, 候補手の列)。"""
        a = self.args
        legal = [cshogi.move_to_usi(m) for m in self.board.legal_moves]
        if a.resign or not legal or (a.resign_after and self.board.move_number > a.resign_after):
            return "resign", None, []
        if a.win:
            return "win", None, []
        rng = self.rng()
        if a.illegal_move:
            legal_set = set(legal)
            for cand in ("1a1b", "9i9h", "5e5d", "1i1a", "9a9i"):
                if cand not in legal_set:
                    return cand, None, [cand]
        legal.sort()
        rng.shuffle(legal)
        best = legal[0]
        ponder = None
        if a.ponder:
            work = self.board.copy()
            work.push_usi(best)
            replies = sorted(cshogi.move_to_usi(m) for m in work.legal_moves)
            if replies:
                ponder = rng.choice(replies)
        return best, ponder, legal

    def finish(self) -> None:
        best, ponder, candidates = self.pick()
        rng = self.rng()
        if self.args.huge_info:
            self.send("info string " + "x" * self.args.huge_info)
        for index, cand in enumerate(candidates[: self.multipv], 1):
            score = rng.randint(-300, 300)
            pv = cand + (f" {ponder}" if ponder and index == 1 else "")
            multipv = f"multipv {index} " if self.multipv > 1 else ""
            self.send(f"info depth 1 seldepth 1 {multipv}score cp {score} nodes 1 nps 1 time 0 pv {pv}")
        self.send(f"bestmove {best}" + (f" ponder {ponder}" if ponder else ""))

    # ---- go の処理 ----

    def search(self, tokens: list[str]) -> bool:
        """探索 1 回分。プロセスを続けるなら True。"""
        a = self.args
        self.go_count += 1
        if a.crash_on_go and self.go_count == a.crash_on_go:
            sys.exit(3)
        if a.crash_rate and self.crash_rng.random() < a.crash_rate:
            sys.exit(3)

        pondering = "ponder" in tokens
        infinite = "infinite" in tokens
        deadline: float | None = None
        if not infinite and (not pondering or a.rogue_ponder_bestmove):
            deadline = time.monotonic() + a.think_ms / 1000

        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            line = self.recv(timeout)
            if line is False:  # 思考時間を使い切った
                self.finish()
                return True
            if line is None:
                return False
            if line == "quit":
                return False
            if line == "stop":
                if a.ignore_stop:
                    deadline = None  # 以後、何も返さない
                    pondering = infinite = False
                    continue
                if a.stop_delay_ms:
                    time.sleep(a.stop_delay_ms / 1000)
                self.finish()
                return True
            if line == "ponderhit" and pondering:
                pondering = False
                deadline = time.monotonic() + a.think_ms / 1000
                continue
            # 探索中のその他のコマンドは無視する

    # ---- メインループ ----

    def run(self) -> int:
        a = self.args
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            line = self.recv(None)
            if line is None:
                return 0 if a.exit_on_eof else self._hang()
            tokens = line.split()
            if not tokens:
                continue
            cmd = tokens[0]
            if cmd == "usi":
                self.send(f"id name {a.name}")
                self.send("id author takeover-tests")
                self.send("option name USI_Ponder type check default false")
                self.send("option name MultiPV type spin default 1 min 1 max 100")
                if not a.no_usiok:
                    self.send("usiok")
            elif cmd == "isready":
                if a.startup_ms:
                    time.sleep(a.startup_ms / 1000)
                if not a.no_readyok:
                    self.send("readyok")
            elif cmd == "setoption":
                if len(tokens) >= 5 and tokens[2] == "MultiPV":
                    try:
                        self.multipv = max(1, int(tokens[4]))
                    except ValueError:
                        pass
            elif cmd == "position":
                try:
                    self.board.set_position(line[len("position "):])
                except Exception:
                    self.send("info string bad position")
            elif cmd == "go":
                if not self.search(tokens):
                    return 0
            elif cmd == "quit":
                return 0
            # usinewgame / gameover / stop（待機中）/ ponderhit（待機中）は何もしない

    def _hang(self) -> int:
        while True:
            time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    return FakeEngine(build_parser().parse_args(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
