"""CsaSession: CSA モードの「対局の外側」（M7。SPEC §6.5、§6.10）。

1 回の起動で、接続画面（名前の選択・追加・編集・削除）→ 接続 → 対局 → 終局、を繰り返す。
連続参加がオンなら、終局後に同じ名前で再 LOGIN して次の対局を待つ。

- 対局 1 局ごとに GameCore と CsaTransport を作り直す。盤面・手番・時計・操作モードを持つのは
  その局の GameCore だけ（I1）。ここが持つのは、名前ストック・選んだ名前・連続参加のオン／オフだけ。
- エンジンは起動したまま次の局でも使う（対局ごとに usinewgame。死んでいたら次の局の前に起動し直す）。
- すべて 1 つのイベントループ上で動かす（I3）。UI からのセッション操作（SessionCommand）は dispatch() で受ける。
- 連続参加が自動で続くのは、対局が成立して終局まで進んだときだけ。接続・LOGIN の失敗や切断のあとは
  再接続せずに接続画面へ戻る（サーバへ繰り返し接続しない）。
- trip / password は UI・ログ・スナップショットに出さない。UI へ渡すのは Account.public_view() だけ。

終了処理（SIGINT / SIGTERM・例外のどれでも通す）:
    棋譜保存 → transport.close() → エンジン quit()（quit → wait → kill → wait）→ ログを閉じる
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
from dataclasses import replace
from pathlib import Path

from . import accounts as acc
from .accounts import Account, AccountError
from .app import (
    ConsoleView,
    _new_log,
    _resolve,
    game_info,
    make_engine,
    parse_stdin_command,
    policy_from,
    split_stdin_lines,
)
from .config import Config, EngineConfig
from .core import events as ev
from .core.game import GameCore
from .core.seat import LocalSeat
from .core.snapshot import Snapshot
from .core.types import Color, EndReason, Mode, NyugyokuRule, Phase
from .engine.base import EngineStartError, EngineState
from .record import kifu
from .transport.csa import CsaSettings, CsaTransport

# セッションの状態（UI の表示用）
LOBBY = "lobby"  # 接続画面（未接続）
CONNECTING = "connecting"  # 接続・LOGIN 中
WAITING = "waiting"  # LOGIN 済みで対局待ち
PLAYING = "playing"  # 対局中（開始待ちを含む）
REJOIN_WAIT = "rejoin_wait"  # 連続参加: 次の接続までの待ち時間


class CsaSession:
    def __init__(self, config: Config, ecfg: EngineConfig, *, accounts: list[Account], accounts_path: Path,
                 initial_alias: str | None, headless: bool, stdin=None, out=None,
                 continuous: bool | None = None) -> None:
        self._config = config
        self._ecfg = ecfg
        self._accounts = list(accounts)
        self._accounts_path = Path(accounts_path)
        self._initial_alias = initial_alias
        self._headless = headless
        self._stdin = stdin
        self._out = out
        self.continuous = config.csa.continuous if continuous is None else continuous
        self.state = LOBBY
        self._alias: str | None = initial_alias  # 接続中（または直前に使った）名前
        self._message = ""
        secrets = tuple(s for a in self._accounts for s in a.secrets())
        self._log = _new_log(config, "csa", secrets=(*secrets, config.ui.lan.token))
        self._last_alias = acc.load_last_alias(self._accounts_path)
        try:
            self._engine = make_engine(config, ecfg, Color.BLACK, self._emit, self._log)  # 色は Game_Summary で決まる
        except EngineStartError:
            self._log.close()
            raise
        self._core: GameCore | None = None
        self._transport: CsaTransport | None = None
        self._view: ConsoleView | None = None
        self._saved = True  # 現在の局の棋譜保存・切断が済んでいるか
        self._shown: Snapshot | None = None  # UI に見せているスナップショット
        self._shown_core: GameCore | None = None
        self._connect_requests: asyncio.Queue[str] = asyncio.Queue()
        self._stop_rejoin = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._gateway = None
        if not headless:
            from .ui.gateway import build_gateway  # UI は使うときだけ読み込む

            self._gateway = build_gateway(config, post_command=self.dispatch, get_snapshot=self._get_snapshot,
                                          get_session=self.view, out=out)

    # ------------------------------------------------------------------ #
    # 表示
    # ------------------------------------------------------------------ #

    def _print(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def _notice(self, text: str) -> None:
        self._log.write("notice", text=text)
        self._print(f"[通知] {text}")
        if self._gateway is not None:
            self._gateway.publish_notice(text)

    def view(self) -> dict:
        """UI へ配るセッションの表示（秘密を含めない）。"""
        selected = self._alias or self._last_alias or self._config.csa.default_account
        if acc.find_account(self._accounts, selected) is None:
            selected = self._accounts[0].alias if self._accounts else None
        return {
            "mode": "csa", "state": self.state, "server": f"{self._config.csa.host}:{self._config.csa.port}",
            "account": self._alias if self.state != LOBBY else None, "selected": selected,
            "accounts": [a.public_view() for a in self._accounts],
            "continuous": self.continuous, "message": self._message,
        }

    def _publish(self) -> None:
        if self._gateway is not None:
            self._gateway.publish_session()

    def _set_state(self, state: str, message: str | None = None) -> None:
        changed = state != self.state or (message is not None and message != self._message)
        self.state = state
        if message is not None:
            self._message = message
        if changed:
            self._log.write("session_state", state=state, account=self._alias)
            self._publish()

    def _get_snapshot(self) -> Snapshot:
        if self._shown is None:
            raise LookupError("まだ表示する対局がありません")
        return self._shown

    # ------------------------------------------------------------------ #
    # GameCore・エンジンとの配線
    # ------------------------------------------------------------------ #

    def _emit(self, event) -> None:
        """エンジン・進行役からのイベントを、いまの局の GameCore へ渡す。"""
        if self._core is not None:
            self._core.post(event)

    def _on_snapshot(self, core: GameCore, snapshot: Snapshot) -> None:
        if core is not self._core:
            return
        if self._view is not None:
            self._view.on_snapshot(snapshot)
        phase = snapshot.phase
        waiting = phase in (Phase.CONNECTING, Phase.WAITING_GAME)
        if phase is Phase.CONNECTING:
            self._set_state(CONNECTING)
        elif phase is Phase.WAITING_GAME:
            self._set_state(WAITING, "対局の成立を待っています")
        elif phase in (Phase.READY, Phase.PLAYING):
            self._set_state(PLAYING, "")
        if waiting and self._shown is not None and self._shown_core is not core:
            return  # 次の対局が決まるまで、前の対局の終局図を見せておく
        self._shown, self._shown_core = snapshot, core
        if self._gateway is not None:
            self._gateway.publish_snapshot(snapshot)

    def _on_core_notice(self, core: GameCore, text: str) -> None:
        if self._view is not None:
            self._view.on_notice(text)
        if self._gateway is not None:
            self._gateway.publish_notice(text)

    def _on_info(self, core: GameCore, color, kind, info) -> None:
        if core is self._core and self._gateway is not None and self._shown_core is core:
            self._gateway.publish_engine_info(color, kind, info)

    # ------------------------------------------------------------------ #
    # UI・標準入力からの操作
    # ------------------------------------------------------------------ #

    def dispatch(self, command: ev.Command) -> None:
        """UiGateway から届いたコマンド。セッション操作はここで処理し、対局の操作は GameCore へ渡す。"""
        if not isinstance(command, ev.SessionCommand):
            # 見せている盤がいまの局のものでないとき（接続画面・次局待ち）は、対局の操作を受けない
            if self._core is not None and self._shown_core is self._core:
                self._core.post(command)
            return
        self._log.write("session_command", command=repr(command))
        if isinstance(command, ev.SetContinuousCommand):
            self.set_continuous(command.enabled)
        elif isinstance(command, ev.ConnectCommand):
            if self.state != LOBBY:
                self._notice("すでに接続しています")
            elif acc.find_account(self._accounts, command.alias) is None:
                self._notice(f"名前 {command.alias!r} が名前ストックにありません")
            else:
                self._connect_requests.put_nowait(command.alias)
        elif isinstance(command, ev.DisconnectCommand):
            self._disconnect()
        elif isinstance(command, ev.SaveAccountCommand):
            self._save_account(command)
        elif isinstance(command, ev.DeleteAccountCommand):
            self._delete_account(command.alias)

    def set_continuous(self, enabled: bool) -> None:
        if enabled == self.continuous:
            return
        self.continuous = enabled
        self._notice("連続参加をオンにしました（終局後に自動で次の対局を待ちます）" if enabled
                     else "連続参加をオフにしました（いまの対局が終わったら接続画面に戻ります）")
        if not enabled and self.state == REJOIN_WAIT:
            self._stop_rejoin.set()
        self._publish()

    def _disconnect(self) -> None:
        core = self._core
        if self.state == REJOIN_WAIT:
            self._stop_rejoin.set()
        elif (self.state in (CONNECTING, WAITING) and core is not None
              and core.phase in (Phase.CONNECTING, Phase.WAITING_GAME)):
            task = asyncio.create_task(self._abort_waiting(core, self._transport))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif self.state == LOBBY:
            pass
        else:
            self._notice("対局中は切断できません（やめるときは投了してください）")

    async def _abort_waiting(self, core: GameCore, transport: CsaTransport | None) -> None:
        """対局待ちをやめる。LOGOUT して接続を閉じ、その局の GameCore を終わらせる。"""
        if core.phase not in (Phase.CONNECTING, Phase.WAITING_GAME):
            self._notice("対局が決まったので、切断しません")
            return
        if transport is not None:
            await transport.close()
        core.post(ev.TransportClosed(detail="（対局待ちをやめました）"))

    def _save_account(self, c: ev.SaveAccountCommand) -> None:
        try:
            updated = acc.upsert_account(
                self._accounts, alias=c.alias, name=c.name, game=c.game, trip=c.trip, password=c.password,
                memo=c.memo, original_alias=c.original_alias)
            acc.save_accounts(self._accounts_path, updated)
        except AccountError as exc:
            self._notice(f"名前を保存できません: {exc}")
            return
        except OSError as exc:
            self._notice(f"名前ストックを書き込めません: {exc}")
            return
        self._accounts = updated
        saved = acc.find_account(updated, c.alias)
        if saved is not None:
            self._log.add_secrets(*saved.secrets())
        if c.original_alias and self._alias == c.original_alias:
            self._alias = c.alias
        self._notice(f"名前 {c.alias!r} を保存しました")
        self._publish()

    def _delete_account(self, alias: str) -> None:
        try:
            updated = acc.delete_account(self._accounts, alias)
            acc.save_accounts(self._accounts_path, updated)
        except AccountError as exc:
            self._notice(f"名前を削除できません: {exc}")
            return
        except OSError as exc:
            self._notice(f"名前ストックを書き込めません: {exc}")
            return
        self._accounts = updated
        self._notice(f"名前 {alias!r} を削除しました")
        self._publish()

    def _on_stdin_line(self, line: str) -> None:
        tokens = line.split()
        if len(tokens) == 2 and tokens[0].lower() == "continuous" and tokens[1].lower() in ("on", "off"):
            self.set_continuous(tokens[1].lower() == "on")
            return
        core = self._core
        if core is None:
            return
        command = parse_stdin_command(line, core.snapshot())
        if isinstance(command, str):
            self._print(command + " / continuous on|off")
        elif command is not None:
            core.post(command)

    # ------------------------------------------------------------------ #
    # 1 局
    # ------------------------------------------------------------------ #

    async def _finish_game(self) -> None:
        """棋譜を保存し、サーバとの接続を閉じる。終局したらすぐ行う（UI を開いたままでも）。"""
        if self._saved:
            return
        self._saved = True
        core, transport, view = self._core, self._transport, self._view
        try:
            if core is not None and core.game_id is not None and (core.moves or core.result is not None):
                when = ((view.started_at if view is not None else None)
                        or dt.datetime.now().astimezone().isoformat(timespec="seconds"))
                cfg = self._config
                paths = kifu.save(_resolve(cfg.base_dir, cfg.record.dir), game_info(core, when), core.moves,
                                  formats=cfg.record.formats, mark_human_moves=cfg.record.mark_human_moves,
                                  score_pov=cfg.csa.comment_score_pov)
                for path in paths:
                    self._print(f"棋譜を保存しました: {path}")
        except Exception as exc:  # 棋譜保存の失敗で片付けを止めない
            self._print(f"棋譜の保存に失敗しました: {exc}")
        try:
            if transport is not None:
                await transport.close()
        except Exception:
            pass

    async def _play_one(self, alias: str) -> bool:
        """alias の名前で接続して 1 局指す。対局が成立して終局まで進んだら True。"""
        account = acc.find_account(self._accounts, alias)
        if account is None:
            self._notice(f"名前 {alias!r} が名前ストックにありません")
            return False
        cfg = self._config
        self._alias = alias
        self._last_alias = alias
        acc.save_last_alias(self._accounts_path, alias)
        self._log.add_secrets(*account.secrets())
        if self._engine.state is EngineState.DEAD:
            try:
                await self._engine.start()
            except EngineStartError as exc:
                self._notice(f"エンジンを起動できません: {exc}")
                return False
        seat = LocalSeat(color=Color.BLACK, name=account.name, engine=self._engine, mode=Mode.AUTO,
                         ponder_enabled=self._ecfg.ponder)
        transport = CsaTransport(
            CsaSettings(
                host=cfg.csa.host, port=cfg.csa.port, login_name=account.name,
                login_password=account.login_password(), x1=False,
                send_engine_comment=cfg.csa.send_engine_comment,
                comment_score_pov=cfg.csa.comment_score_pov, keepalive_sec=cfg.csa.keepalive_sec,
                connect_timeout_ms=cfg.csa.connect_timeout_ms, agree_timeout_ms=cfg.csa.agree_timeout_ms,
            ),
            emit=self._emit, log=self._log,
        )
        policy = replace(policy_from(cfg), nyugyoku_rule=NyugyokuRule.LAW_27)  # CSA サーバは 27 点法
        holder: list[GameCore] = []
        core = GameCore(
            my_seat=seat, transport=transport, policy=policy, log=self._log,
            on_snapshot=lambda s: self._on_snapshot(holder[0], s),
            on_notice=lambda text: self._on_core_notice(holder[0], text),
            on_engine_info=lambda color, kind, info: self._on_info(holder[0], color, kind, info),
        )
        holder.append(core)
        self._core, self._transport, self._view = core, transport, ConsoleView(self._out)
        self._saved = False
        self._print(f"{cfg.csa.host}:{cfg.csa.port} に {account.name}（{account.alias}）で参加します")
        self._set_state(CONNECTING, f"{account.name} で接続しています")
        await transport.start()
        await core.run()
        await self._finish_game()
        return core.game_id is not None and core.end_reason is not EndReason.DISCONNECTED

    async def _wait_before_rejoin(self) -> bool:
        """連続参加: 次の接続まで待つ。途中でやめたら False。"""
        delay = max(0, self._config.csa.rejoin_delay_sec)
        self._stop_rejoin.clear()
        self._set_state(REJOIN_WAIT, f"連続参加: {delay} 秒後に次の対局を待ちます")
        self._print(f"連続参加: {delay} 秒後に再接続します")
        try:
            await asyncio.wait_for(self._stop_rejoin.wait(), delay)
        except asyncio.TimeoutError:
            return self.continuous
        return False

    # ------------------------------------------------------------------ #
    # 全体
    # ------------------------------------------------------------------ #

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        installed: list[int] = []
        cleaning = False

        def on_signal() -> None:
            # 片付けが始まったら無視する（app._run_game と同じ理由）
            if not cleaning:
                main_task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_signal)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        reader_fd = None
        ui_task = None
        code = 0
        try:
            try:
                await self._engine.start()
            except EngineStartError as exc:
                self._print(f"エラー: {exc}")
                return 1
            if self._stdin is not None:
                pending = bytearray()

                def on_stdin() -> None:
                    try:
                        data = os.read(reader_fd, 65536)
                    except (BlockingIOError, InterruptedError):
                        return
                    except OSError:
                        data = b""
                    if not data:  # EOF
                        loop.remove_reader(reader_fd)
                        data = b"\n" if pending else b""
                    for line in split_stdin_lines(pending, data):
                        self._on_stdin_line(line)

                try:
                    reader_fd = self._stdin.fileno()
                    loop.add_reader(reader_fd, on_stdin)
                except (OSError, ValueError, NotImplementedError):
                    reader_fd = None
            if self._gateway is not None:
                ui_task = asyncio.create_task(self._gateway.serve())

            alias = self._initial_alias
            while True:
                if alias is None:
                    if self._gateway is None:
                        break  # ヘッドレスは 1 局（連続参加なら続く限り）で終わる
                    self._set_state(LOBBY, self._message)
                    alias = await self._connect_requests.get()
                played = await self._play_one(alias)
                code = 0 if played else 1
                if played and self.continuous and await self._wait_before_rejoin():
                    continue
                if self._gateway is not None:
                    self._print("対局が終わりました。UI は開いたままです（次の対局は UI の「接続」から。Ctrl-C で終了）"
                                if played else "接続画面に戻ります（Ctrl-C で終了）")
                    last = self._core
                    reason = "" if played or last is None or not last.notices else last.notices[-1]
                    self._message = reason
                alias = None
        except asyncio.CancelledError:
            core = self._core
            if core is not None and core.phase is Phase.PLAYING:
                self._print("対局中に中断されました（サーバとの接続を切るので、この対局は負けになります）")
            code = 130
        finally:
            cleaning = True
            if reader_fd is not None:
                try:
                    loop.remove_reader(reader_fd)
                except Exception:
                    pass
            for task in list(self._tasks):
                task.cancel()
            if ui_task is not None and not ui_task.done():
                ui_task.cancel()
                try:
                    await ui_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._finish_game()
            await asyncio.gather(self._engine.quit(), return_exceptions=True)
            self._log.close()
            for sig in installed:
                loop.remove_signal_handler(sig)
        return code
