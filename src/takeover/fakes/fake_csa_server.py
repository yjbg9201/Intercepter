"""偽 CSA サーバ。テストから in-process で起動するほか、`python -m takeover fake-server` でも動く。

参照: SPEC §9「偽 CSA サーバ」、§6.5、§6.4（時計の規則）

- asyncio.start_server で実装する（port=0 で空きポート）。1 接続 1 局。
- LOGIN → Game_Summary → AGREE → START → `<指し手>,T<n>` のエコー → 終局通知。
- 時計は shogi-server と同じ規則（加算 → 減算、秒未満切り捨て、時間切れ判定）。
- 相手はランダム合法手（seed・遅延を指定可）。台本（指し手の列）で指させることもできる。
- %TORYO / %KACHI（27 点法で判定）/ 千日手 / 最大手数。
- テストから参照できる記録:
    received_lines   … クライアントから受信した全行（コメントを含む生の行）
    deferred_moves   … 手番外に届いて保留した指し手（本物と同じ挙動。誤送信の検出用）
    login            … 受け取った (名前, パスワード, x1 か)
    result_lines     … 終局時にクライアントへ送った `#` 行
- 空行には空行で応答する（キープアライブ）。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import cshogi

from ..core import clock as clockmod
from ..core.nyugyoku import can_declare
from ..core.rules import RepetitionTracker
from ..core.types import NyugyokuRule, TimeControl


@dataclass(slots=True)
class FakeServerConfig:
    host: str = "127.0.0.1"
    port: int = 0  # 0 は空きポート
    total_sec: int = 300
    byoyomi_sec: int = 0
    increment_sec: int = 10
    max_moves: int = 256
    client_color: str = "+"  # クライアントに割り当てる手番（"+" / "-"）
    opponent_name: str = "fake_opponent"
    opponent_delay_ms: int = 0
    opponent_moves: list[str] = field(default_factory=list)  # 台本（USI）。尽きたらランダム
    opponent_resign_after: int = 0  # 相手はこの手数以降の手番で投了する（0 は無効）
    opponent_comment: str = ""  # 空でなければ、相手の手にこのコメントを付けて中継する（本物はしない）
    accept_login: bool = True
    reject_game: bool = False  # AGREE の後に REJECT を返す
    seed: int = 0
    game_id: str = "fake-game-1"
    match_delay_sec: float = 0  # LOGIN から Game_Summary を送るまでの待ち（対局待ちの再現）
    start_sfen: str | None = None


class FakeCsaServer:
    def __init__(self, config: FakeServerConfig | None = None) -> None:
        self.config = config or FakeServerConfig()
        self.received_lines: list[str] = []
        self.sent_lines: list[str] = []
        self.deferred_moves: list[str] = []
        self.result_lines: list[str] = []
        self.login: tuple[str, str, bool] | None = None
        self.logins: list[tuple[str, str, bool]] = []  # 接続ごとの LOGIN（連続参加の確認用）
        self.game_requests: list[str] = []  # 拡張モードで受け取った %%GAME / %%CHALLENGE
        self.moves: list[str] = []  # 確定した指し手（USI）
        self._server: asyncio.base_events.Server | None = None
        self._game_end = asyncio.Event()
        self._client_turn = False
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.once = True  # False なら 1 局終わっても次の接続を受ける

    @property
    def port(self) -> int:
        """実際に待ち受けているポート（start() の後で有効）。"""
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.config.host, self.config.port)

    async def wait_game_end(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._game_end.wait(), timeout)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------ #

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._session(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._game_end.set()
            self._writers.discard(writer)
            self._tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    def _send(self, writer: asyncio.StreamWriter, line: str) -> None:
        self.sent_lines.append(line)
        writer.write((line + "\n").encode("utf-8"))

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cfg = self.config
        inbox: asyncio.Queue[str | None] = asyncio.Queue()

        async def pump() -> None:
            while True:
                raw = await reader.readline()
                if not raw:
                    await inbox.put(None)
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self.received_lines.append(line)
                if line == "":
                    self._send(writer, "")  # キープアライブ
                    await writer.drain()
                    continue
                game_line = line[:1] in "+-" or (line.startswith("%") and not line.startswith("%%"))
                if game_line and not self._client_turn and self._playing:
                    # 本物は手番外の指し手・%X を捨てずに保留し、次の手番に適用する（下で inbox に入れる）
                    self.deferred_moves.append(line)
                if line.startswith("%%"):
                    if self._x1 and not self._playing and line.split()[0] in ("%%GAME", "%%CHALLENGE"):
                        await inbox.put(line)  # 拡張モード: 対局の申し込み
                        continue
                    name = line[2:].split()[0] if line[2:].split() else ""
                    self._send(writer, f"##[{name}] +OK")  # 拡張コマンドは対局の進行に影響しない
                    await writer.drain()
                    continue
                if not game_line and line.split()[0] not in ("LOGIN", "LOGOUT", "AGREE", "REJECT"):
                    self._send(writer, f"##[ERROR] unknown command: {line}")  # 本物の ErrorCommand と同じ
                    await writer.drain()
                    continue
                await inbox.put(line)

        self._playing = False
        self._x1 = False
        pump_task = asyncio.create_task(pump())
        self._tasks.add(pump_task)
        try:
            line = await inbox.get()
            tokens = (line or "").split()
            if len(tokens) < 3 or tokens[0] != "LOGIN":
                return
            name = tokens[1]
            self.login = (name, tokens[2], len(tokens) > 3 and tokens[3] == "x1")
            self.logins.append(self.login)
            if not cfg.accept_login:
                self._send(writer, "LOGIN:incorrect")
                await writer.drain()
                return
            self._send(writer, f"LOGIN:{name} OK")
            self._x1 = self.login[2]
            if self._x1:
                # 拡張モード（x1）: LOGIN だけでは対局待ちにならない。%%GAME / %%CHALLENGE を待つ
                self._send(writer, "##[LOGIN] +OK x1")
                await writer.drain()
                while True:
                    line = await inbox.get()
                    if line is None or line == "LOGOUT":
                        return
                    if line.startswith(("%%GAME", "%%CHALLENGE")):
                        self.game_requests.append(line)
                        break
            await writer.drain()
            if cfg.match_delay_sec > 0:
                try:
                    line = await asyncio.wait_for(inbox.get(), cfg.match_delay_sec)
                    if line is None or line == "LOGOUT":
                        return  # 対局が決まる前にクライアントがやめた
                except asyncio.TimeoutError:
                    pass

            board = cshogi.Board(sfen=cfg.start_sfen) if cfg.start_sfen else cshogi.Board()
            black, white = (name, cfg.opponent_name) if cfg.client_color == "+" else (cfg.opponent_name, name)
            for summary_line in self._summary(board, black, white):
                self._send(writer, summary_line)
            await writer.drain()

            while True:
                line = await inbox.get()
                if line is None or line == "LOGOUT":
                    return
                if line.startswith("AGREE"):
                    break
                if line.startswith("REJECT"):
                    self._send(writer, f"REJECT:{cfg.game_id} by {name}")
                    await writer.drain()
                    return
            if cfg.reject_game:
                self._send(writer, f"REJECT:{cfg.game_id} by {cfg.opponent_name}")
                await writer.drain()
                return
            self._send(writer, f"START:{cfg.game_id}")
            await writer.drain()
            await self._play(board, inbox, writer)
            await writer.drain()
            # クライアントの LOGOUT か切断を少し待つ
            try:
                while True:
                    line = await asyncio.wait_for(inbox.get(), 1.0)
                    if line is None or line == "LOGOUT":
                        break
            except asyncio.TimeoutError:
                pass
        finally:
            self._playing = False
            pump_task.cancel()
            self._tasks.discard(pump_task)

    def _summary(self, board, black: str, white: str) -> list[str]:
        cfg = self.config
        to_move = "+" if board.turn == cshogi.BLACK else "-"
        lines = [
            "BEGIN Game_Summary",
            "Protocol_Version:1.2",
            "Protocol_Mode:Server",
            "Format:Shogi 1.0",
            "Declaration:Jishogi 1.1",
            f"Game_ID:{cfg.game_id}",
            f"Name+:{black}",
            f"Name-:{white}",
            f"Your_Turn:{cfg.client_color}",
            "Rematch_On_Draw:NO",
            f"To_Move:{to_move}",
            f"Max_Moves:{cfg.max_moves}",
            "BEGIN Time",
            "Time_Unit:1sec",
            f"Total_Time:{cfg.total_sec}",
            f"Byoyomi:{cfg.byoyomi_sec}",
            f"Increment:{cfg.increment_sec}",
            "Least_Time_Per_Move:0",
            "END Time",
            "BEGIN Position",
        ]
        lines += [l for l in board.csa_pos().split("\n") if l]
        lines += ["END Position", "END Game_Summary"]
        return lines

    def _end(self, writer: asyncio.StreamWriter, reason: str, client_result: str) -> None:
        for line in (reason, client_result):
            self.result_lines.append(line)
            self._send(writer, line)

    async def _play(self, board, inbox: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        cfg = self.config
        tc = TimeControl(total_ms=cfg.total_sec * 1000, byoyomi_ms=cfg.byoyomi_sec * 1000,
                         increment_ms=cfg.increment_sec * 1000)
        remaining = {"+": tc.total_ms, "-": tc.total_ms}
        rng = random.Random(cfg.seed)
        script = list(cfg.opponent_moves)
        repetition = RepetitionTracker(board)
        self._playing = True

        def won(mover: str, mover_wins: bool) -> str:
            client_wins = (mover == cfg.client_color) == mover_wins
            return "#WIN" if client_wins else "#LOSE"

        while True:
            mover = "+" if board.turn == cshogi.BLACK else "-"
            started = time.monotonic()
            self._client_turn = mover == cfg.client_color
            comment = ""

            if self._client_turn:
                limit = clockmod.flag_after_ms(remaining[mover], tc)
                try:
                    line = await asyncio.wait_for(inbox.get(), None if limit is None else limit / 1000)
                except asyncio.TimeoutError:
                    self._end(writer, "#TIME_UP", "#LOSE")
                    return
                if line is None:
                    return
                if line == "LOGOUT":
                    return
                head = line.split(",")[0]
            else:
                if cfg.opponent_delay_ms:
                    await asyncio.sleep(cfg.opponent_delay_ms / 1000)
                legal = sorted(cshogi.move_to_usi(m) for m in board.legal_moves)
                if not legal or (cfg.opponent_resign_after and board.move_number > cfg.opponent_resign_after):
                    head = "%TORYO"
                else:
                    usi = script.pop(0) if script else rng.choice(legal)
                    head = mover + cshogi.move_to_csa(board.move_from_usi(usi))
                comment = cfg.opponent_comment

            elapsed_ms = int((time.monotonic() - started) * 1000)
            t = clockmod.consumed_ms(elapsed_ms, tc)
            if clockmod.is_time_up(remaining[mover], t, tc):
                self._end(writer, "#TIME_UP", won(mover, False))
                return
            remaining[mover] = max(0, remaining[mover] + tc.increment_ms - t)
            seconds = t // 1000

            if head == "%TORYO":
                self._send(writer, f"%TORYO,T{seconds}")
                self._end(writer, "#RESIGN", won(mover, False))
                return
            if head == "%KACHI":
                self._send(writer, f"%KACHI,T{seconds}")
                if can_declare(board, NyugyokuRule.LAW_27):
                    self._end(writer, "#JISHOGI", won(mover, True))
                else:
                    self._end(writer, "#ILLEGAL_MOVE", won(mover, False))
                return
            if head == "%CHUDAN":
                self._end(writer, "#CHUDAN", "#CHUDAN")
                return
            if head.startswith("%"):
                # 本物（shogi-server）は %TORYO / %KACHI 以外の %X を知らない＝非合法手として扱う
                self._end(writer, "#ILLEGAL_MOVE", won(mover, False))
                return

            move = 0
            if len(head) == 7 and head[0] == mover:
                try:
                    move = board.move_from_csa(head[1:])
                except Exception:
                    move = 0
            if not move or not board.is_legal(move):
                self._end(writer, "#ILLEGAL_MOVE", won(mover, False))
                return

            self.moves.append(cshogi.move_to_usi(move))
            board.push(move)
            self._client_turn = False
            echo = f"{head},T{seconds}"
            if comment and mover != cfg.client_color:
                echo += f",{comment}"
            self._send(writer, echo)
            await writer.drain()

            rep = repetition.push(board)
            if rep is not None:
                if rep.perpetual_checker is None:
                    self._end(writer, "#SENNICHITE", "#DRAW")
                else:
                    checker = "+" if rep.perpetual_checker.value == "b" else "-"
                    self._end(writer, "#OUTE_SENNICHITE", "#LOSE" if checker == cfg.client_color else "#WIN")
                return
            if cfg.max_moves and len(self.moves) >= cfg.max_moves:
                self._end(writer, "#MAX_MOVES", "#CENSORED")
                return


async def serve_forever(config: FakeServerConfig) -> None:
    """CLI（`python -m takeover fake-server`）用。Ctrl-C まで対局を受け付け続ける。"""
    server = FakeCsaServer(config)
    await server.start()
    print(f"fake CSA server listening on {config.host}:{server.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
