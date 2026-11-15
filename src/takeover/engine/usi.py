"""EngineController: USI エンジンの子プロセスの唯一の所有者（I2）。engine.base.Engine の実装。

参照: SPEC §6.3（状態機械・規則）、§7.4（異常時）、CLAUDE.md「過去実装で踏んだ不具合」

- stdout を読むのは専用タスク 1 つだけ。各行の `\\r` を除去してから usi_parse に渡す。
- stderr は DEVNULL（I9）。StreamReader の limit は設定値。
- 探索ごとに search_id を振る。STOPPING 中に来た bestmove は破棄する（I5）。
- IDLE 以外で think / ponder / analyze が呼ばれたら、送信せず EngineBusyError（I6）。
- PONDERING / ANALYZING 中に stop / ponderhit なしで来た bestmove は破棄して IDLE に戻る。
- cancel(): stop → stop_grace_ms 待つ → 来なければ kill して return。再起動は裏のタスクで行い、
  EngineReadyEvent / EngineDeadEvent を emit する。再起動は 1 局 max_restarts 回まで。
- 送受信した生の行と、search_id の払い出し・破棄した bestmove をイベントログへ（I10）。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..core.events import BestMoveEvent, EngineDeadEvent, EngineInfoEvent, EngineReadyEvent
from ..core.types import Color
from . import usi_parse
from .base import EmitFn, EngineBusyError, EngineError, EngineStartError, EngineState, GoTime
from .launch import EngineKind, LaunchSpec, ensure_windows_process_gone

_SEARCHING = (EngineState.THINKING, EngineState.PONDERING, EngineState.ANALYZING)


@dataclass(frozen=True, slots=True)
class EngineTimeouts:
    """すべて設定値から渡す（ハードコードしない）。"""

    usi_timeout_ms: int
    stop_grace_ms: int
    quit_timeout_ms: int
    max_restarts: int
    line_limit_bytes: int
    info_interval_ms: int = 200


class _NoLog:
    def write(self, kind: str, **fields) -> None:
        pass


class EngineController:
    def __init__(
        self,
        *,
        color: Color,
        launch: LaunchSpec,
        options: Mapping[str, str | int | bool],
        ponder_enabled: bool,
        timeouts: EngineTimeouts,
        emit: EmitFn,
        log=None,  # record.eventlog.EventLog
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._color = color
        self._launch = launch
        self._options: dict[str, str | int | bool] = {"USI_Ponder": ponder_enabled, **options}
        self._timeouts = timeouts
        self._emit = emit
        self._log = log if log is not None else _NoLog()
        self._now = now
        self._state = EngineState.DEAD
        self._name = launch.display_name
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._generation = 0  # プロセスの世代。古い読み取りタスクの終了通知を無視するため
        self._waiter: tuple[str, asyncio.Future] | None = None
        self._not_searching = asyncio.Event()
        self._not_searching.set()
        self._next_search_id = 0
        self._search_id: int | None = None
        self._last_info: usi_parse.ParsedInfo | None = None
        self._last_info_emit: dict[int, float] = {}
        self._restarts = 0
        self._in_game = False
        self._quitting = False

    # ------------------------------------------------------------------ #
    # 参照
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def current_search_id(self) -> int | None:
        return self._search_id

    async def wait_not_searching(self, timeout_ms: int | None = None) -> None:
        """探索中でなくなるまで待つ（主にテスト用）。"""
        timeout = None if timeout_ms is None else timeout_ms / 1000
        await asyncio.wait_for(self._not_searching.wait(), timeout)

    # ------------------------------------------------------------------ #
    # 起動・終了
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._state is not EngineState.DEAD:
            raise EngineError(f"start() called while {self._state.value}")
        self._quitting = False
        self._state = EngineState.STARTING
        try:
            await self._spawn_and_handshake()
        except EngineStartError:
            await self._kill()
            self._state = EngineState.DEAD
            raise
        self._state = EngineState.IDLE

    async def _spawn_and_handshake(self) -> None:
        spec = self._launch
        self._log.write("engine_spawn", color=self._color, argv=list(spec.argv), cwd=spec.cwd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                cwd=spec.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # I9
                limit=self._timeouts.line_limit_bytes,
                start_new_session=True,  # 端末の Ctrl-C を直接受けさせない（quit で片付ける）
            )
        except OSError as exc:
            raise EngineStartError(f"エンジン {self._name!r} を起動できません: {exc}") from None
        self._proc = proc
        self._generation += 1
        self._reader_task = asyncio.create_task(self._read_loop(proc, self._generation))
        await self._send("usi")
        await self._expect("usiok")
        for line in usi_parse.build_setoptions(self._options):
            await self._send(line)
        await self._send("isready")
        await self._expect("readyok")

    async def _expect(self, token: str) -> None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiter = (token, future)
        try:
            await asyncio.wait_for(future, self._timeouts.usi_timeout_ms / 1000)
        except asyncio.TimeoutError:
            raise EngineStartError(
                f"エンジン {self._name!r}: {token} が {self._timeouts.usi_timeout_ms} ms 以内に返りません"
            ) from None
        finally:
            self._waiter = None

    async def _kill(self) -> None:
        """プロセスを強制終了して wait する。読み取りタスクの終了通知は無視させる。"""
        proc, self._proc = self._proc, None
        reader, self._reader_task = self._reader_task, None
        self._generation += 1
        if proc is not None:
            killed = proc.returncode is None  # ログには実際に kill したかどうかを残す
            if killed:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            self._log.write("engine_exit", color=self._color, returncode=proc.returncode, killed=killed)
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass

    async def quit(self) -> None:
        self._quitting = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except (asyncio.CancelledError, Exception):
                pass
            self._restart_task = None
        proc = self._proc
        timeout = self._timeouts.quit_timeout_ms / 1000
        if proc is not None and proc.returncode is None:
            await self._send("quit")
            if not await self._wait_exit(proc, timeout):
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()  # EOF で終了するエンジンが多い
                except Exception:
                    pass
                await self._wait_exit(proc, timeout)
        await self._kill()  # 残っていれば kill → wait。終わっていれば wait だけ
        if self._launch.kind is EngineKind.WINDOWS:
            await ensure_windows_process_gone(self._launch, None)
        self._search_id = None
        self._not_searching.set()
        self._state = EngineState.DEAD

    @staticmethod
    async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ #
    # 送受信
    # ------------------------------------------------------------------ #

    async def _send(self, line: str) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return False
        self._log.write("usi_send", color=self._color, line=line)
        try:
            proc.stdin.write((line + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # プロセス死亡と同じ扱い。読み取りタスクが EOF を見て _on_exit で処理する
            self._log.write("usi_send_failed", color=self._color, line=line, error=repr(exc))
            return False
        return True

    async def _read_loop(self, proc: asyncio.subprocess.Process, generation: int) -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # 行が上限を超えた。その行は捨てて読み取りを続ける（SPEC §6.3）
                self._log.write("usi_line_too_long", color=self._color)
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n").replace("\r", "")
            self._log.write("usi_recv", color=self._color, line=line)
            if generation == self._generation:
                self._on_line(line)
        await proc.wait()
        if generation == self._generation:
            self._on_exit(proc.returncode)

    def _on_line(self, line: str) -> None:
        if not line:
            return
        if self._waiter is not None and line.strip() == self._waiter[0]:
            if not self._waiter[1].done():
                self._waiter[1].set_result(None)
            return
        if line.startswith("bestmove"):
            self._on_bestmove(line)
        elif line.startswith("info"):
            self._on_info(line)
        elif line.startswith("id "):
            parsed = usi_parse.parse_id(line)
            if parsed and parsed[0] == "name":
                self._name = parsed[1]

    def _on_info(self, line: str) -> None:
        if self._search_id is None or self._state not in _SEARCHING:
            return
        info = usi_parse.parse_info(line)
        if info is None or (info.score is None and not info.pv):
            return
        if info.multipv == 1:
            self._last_info = info
        now = self._now()
        last = self._last_info_emit.get(info.multipv)
        if last is not None and (now - last) * 1000 < self._timeouts.info_interval_ms:
            return
        self._last_info_emit[info.multipv] = now
        self._emit(EngineInfoEvent(
            color=self._color, search_id=self._search_id, depth=info.depth, seldepth=info.seldepth,
            score=info.score, nodes=info.nodes, nps=info.nps, hashfull=info.hashfull,
            multipv=info.multipv, pv=info.pv,
        ))

    def _on_bestmove(self, line: str) -> None:
        parsed = usi_parse.parse_bestmove(line)
        state, search_id = self._state, self._search_id
        if state not in _SEARCHING and state is not EngineState.STOPPING:
            self._log.write("bestmove_discarded", color=self._color, line=line, reason=f"state={state.value}")
            return
        info, self._last_info = self._last_info, None
        self._search_id = None
        self._state = EngineState.IDLE
        self._not_searching.set()
        if state is EngineState.THINKING and parsed is not None and search_id is not None:
            self._emit(BestMoveEvent(
                color=self._color, search_id=search_id, move=parsed.move, ponder=parsed.ponder,
                score=info.score if info else None, pv=info.pv if info else (),
            ))
            return
        # STOPPING（cancel 済み）、PONDERING / ANALYZING 中の違反 bestmove は破棄する（I5、SPEC §6.3）
        self._log.write("bestmove_discarded", color=self._color, line=line, search_id=search_id,
                        reason=f"state={state.value}")

    def _on_exit(self, returncode: int | None) -> None:
        """プロセスが勝手に終了した（読み取りタスクから呼ばれる）。"""
        self._log.write("engine_exit", color=self._color, returncode=returncode, state=self._state.value)
        if self._quitting:
            return
        if self._waiter is not None and not self._waiter[1].done():
            self._waiter[1].set_exception(
                EngineStartError(f"エンジン {self._name!r} が起動中に終了しました（終了コード {returncode}）")
            )
        if self._state in (EngineState.STARTING, EngineState.DEAD):
            return  # 起動・再起動の途中。ハンドシェイク側が処理する
        self._search_id = None
        self._not_searching.set()
        self._begin_restart(f"process exited (code {returncode})")

    # ------------------------------------------------------------------ #
    # 再起動
    # ------------------------------------------------------------------ #

    def _begin_restart(self, reason: str) -> None:
        if self._quitting:
            self._state = EngineState.DEAD
            return
        if self._restarts < self._timeouts.max_restarts:
            self._restarts += 1
            self._state = EngineState.STARTING
            self._log.write("engine_restart", color=self._color, reason=reason, attempt=self._restarts)
            self._emit(EngineDeadEvent(color=self._color, reason=reason, will_restart=True))
            self._restart_task = asyncio.create_task(self._restart())
        else:
            self._state = EngineState.DEAD
            self._log.write("engine_dead", color=self._color, reason=reason)
            self._emit(EngineDeadEvent(color=self._color, reason=reason, will_restart=False))

    async def _restart(self) -> None:
        try:
            await self._kill()
            await self._spawn_and_handshake()
            if self._in_game:
                await self._send("usinewgame")
        except EngineStartError as exc:
            await self._kill()
            self._state = EngineState.DEAD
            self._log.write("engine_dead", color=self._color, reason=str(exc))
            self._emit(EngineDeadEvent(color=self._color, reason=str(exc), will_restart=False))
            return
        self._state = EngineState.IDLE
        self._emit(EngineReadyEvent(color=self._color, restarted=True))

    # ------------------------------------------------------------------ #
    # 対局
    # ------------------------------------------------------------------ #

    async def new_game(self) -> None:
        self._in_game = True
        self._restarts = 0
        if self._state is EngineState.IDLE:
            await self._send("usinewgame")

    async def game_over(self, result: str) -> None:
        self._in_game = False
        if self._state is EngineState.IDLE:
            await self._send(f"gameover {result}")

    def _begin_search(self, method: str, state: EngineState) -> int:
        if self._state is not EngineState.IDLE:
            raise EngineBusyError(f"{method}() called while {self._state.value}（I6）")
        self._next_search_id += 1
        self._search_id = self._next_search_id
        self._state = state
        self._last_info = None
        self._last_info_emit.clear()
        self._not_searching.clear()
        self._log.write("search_start", color=self._color, search_id=self._search_id, search=method)
        return self._search_id

    async def think(self, position_cmd: str, go_time: GoTime) -> int:
        search_id = self._begin_search("think", EngineState.THINKING)
        await self._send(position_cmd)
        await self._send(usi_parse.build_go(go_time))
        return search_id

    async def ponder(self, position_cmd: str, go_time: GoTime) -> int:
        search_id = self._begin_search("ponder", EngineState.PONDERING)
        await self._send(position_cmd)
        await self._send(usi_parse.build_go(go_time, ponder=True))
        return search_id

    async def ponderhit(self) -> None:
        if self._state is not EngineState.PONDERING:
            raise EngineError(f"ponderhit() called while {self._state.value}")
        self._state = EngineState.THINKING
        await self._send("ponderhit")

    async def analyze(self, position_cmd: str) -> int:
        search_id = self._begin_search("analyze", EngineState.ANALYZING)
        await self._send(position_cmd)
        await self._send("go infinite")
        return search_id

    async def cancel(self) -> None:
        if self._state in _SEARCHING:
            self._state = EngineState.STOPPING
            self._log.write("search_cancel", color=self._color, search_id=self._search_id)
            await self._send("stop")
        if self._state is not EngineState.STOPPING:
            return
        try:
            await asyncio.wait_for(self._not_searching.wait(), self._timeouts.stop_grace_ms / 1000)
        except asyncio.TimeoutError:
            pass
        if self._state is EngineState.STOPPING:
            # stop に応答しない。強制終了して戻る。再起動は裏で行う（SPEC §6.3）
            self._log.write("stop_timeout", color=self._color, search_id=self._search_id)
            self._search_id = None
            await self._kill()
            self._not_searching.set()
            self._begin_restart("stop timeout")

    async def set_options(self, options: Mapping[str, str | int | bool]) -> None:
        if self._state is not EngineState.IDLE:
            raise EngineBusyError(f"set_options() called while {self._state.value}")
        self._options.update(options)  # 再起動時にも同じ値を送る
        for line in usi_parse.build_setoptions(options):
            await self._send(line)
