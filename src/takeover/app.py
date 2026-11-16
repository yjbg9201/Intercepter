"""組み立てと実行: 設定から各コンポーネントを作って配線し、終了時に片付ける。

参照: SPEC §5.1（全体図）、§6.5（接続の順序・終了処理）

配線（どのモードでも同じ形）:
    EventLog → エンジン（EngineController）→ 進行役（LocalReferee か CsaTransport）
    → GameCore → （UI があれば）UiGateway。すべて 1 つのイベントループ上で動かす（I3）。

CSA モードの対局の繰り返し（接続画面 → 接続 → 対局 → 終局 → 連続参加）は session.py の CsaSession が受け持つ。
ここの _run_game はローカル対局（1 起動 1 局）用。

終了処理（正常終局・SIGINT / SIGTERM・例外のどれでも必ず通す）:
    棋譜保存 → transport.close() → 全エンジン quit()（quit → wait → kill → wait）→ ログを閉じる

ヘッドレスの標準入力コマンド:
    take b|w / release b|w / move <usi> / resign [b|w] / declare [b|w] / hint on|off / hint multi on|off
    標準入力は loop.add_reader で同じループから読む（スレッドを使わない）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
import sys
from pathlib import Path

from .config import Config, ConfigError, EngineConfig, engine_config, load_config
from .core import events as ev
from .core.game import GameCore, GamePolicy
from .core.seat import LocalSeat
from .core.snapshot import Snapshot
from .core.types import Color, Mode, NyugyokuRule, Phase, TimeControl
from .engine.base import EngineStartError
from .engine.launch import resolve_launch
from .engine.usi import EngineController, EngineTimeouts
from .record import kifu
from .record.eventlog import EventLog
from .transport.local import LocalReferee

HELP = "コマンド: take b|w / release b|w / move <usi> / resign [b|w] / declare [b|w] / hint on|off / hint multi on|off"


def policy_from(config: Config) -> GamePolicy:
    return GamePolicy(
        margin_ms=config.time.margin_ms,
        time_guard_min_remaining_ms=config.time_guard.min_remaining_ms,
        time_guard_action=config.time_guard.action,
        on_engine_resign=config.policy.on_engine_resign,
        on_engine_failure=config.policy.on_engine_failure,
        nyugyoku_rule=NyugyokuRule(config.local.nyugyoku_rule),
        hint_enabled=config.hint.enabled,
        hint_multipv=config.hint.multipv,
        hint_send_as_comment=config.hint.send_as_comment,
        schedule=tuple((s.first_ply, s.last_ply) for s in config.schedule),
    )


def timeouts_from(config: Config) -> EngineTimeouts:
    p = config.policy
    return EngineTimeouts(
        usi_timeout_ms=p.usi_timeout_ms, stop_grace_ms=p.stop_grace_ms, quit_timeout_ms=p.quit_timeout_ms,
        max_restarts=p.max_restarts, line_limit_bytes=p.usi_line_limit_bytes,
        info_interval_ms=p.engine_info_interval_ms,
    )


def make_engine(config: Config, ecfg: EngineConfig, color: Color, emit, log) -> EngineController:
    spec = resolve_launch(name=ecfg.name, path=ecfg.path, cwd=ecfg.cwd, kind=ecfg.kind, args=ecfg.args)
    return EngineController(color=color, launch=spec, options=ecfg.options, ponder_enabled=ecfg.ponder,
                            timeouts=timeouts_from(config), emit=emit, log=log)


def split_stdin_lines(buffer: bytearray, data: bytes) -> list[str]:
    """標準入力から読んだバイト列を buffer に足し、完成した行だけを取り出す（未完の行は buffer に残す）。

    readline() は使わない: 複数行が一度に届くと 2 行目以降が Python 側のバッファに入り、
    fd は読み取り可能にならないので、次の入力が来るまで処理されなくなる。
    """
    buffer += data
    *lines, rest = bytes(buffer).split(b"\n")
    buffer[:] = rest
    return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]


def parse_stdin_command(line: str, snapshot: Snapshot) -> ev.Command | str | None:
    """標準入力の 1 行をコマンドにする（純関数）。空行は None、解釈できなければ説明文を返す。"""
    tokens = line.split()
    if not tokens:
        return None
    colors = {"b": Color.BLACK, "w": Color.WHITE, "+": Color.BLACK, "-": Color.WHITE}
    default_color = snapshot.my_color or snapshot.turn

    def color_arg(index: int) -> Color | None:
        if len(tokens) > index:
            return colors.get(tokens[index].lower())
        return default_color

    cmd = tokens[0].lower()
    if cmd in ("take", "release"):
        color = color_arg(1)
        if color is None:
            return HELP
        return ev.SetModeCommand(color=color, mode=Mode.MANUAL if cmd == "take" else Mode.AUTO, client_id="stdin")
    if cmd == "move" and len(tokens) >= 2:
        return ev.MoveCommand(color=default_color, ply=snapshot.ply, usi=tokens[1], client_id="stdin")
    if cmd in ("resign", "declare"):
        color = color_arg(1)
        if color is None:
            return HELP
        cls = ev.ResignCommand if cmd == "resign" else ev.DeclareCommand
        return cls(color=color, ply=snapshot.ply, client_id="stdin")
    if cmd == "hint" and len(tokens) >= 3 and tokens[1].lower() == "multi" and tokens[2].lower() in ("on", "off"):
        return ev.SetHintMultiPvCommand(enabled=tokens[2].lower() == "on", client_id="stdin")
    if cmd == "hint" and len(tokens) >= 2 and tokens[1].lower() in ("on", "off"):
        return ev.SetHintCommand(enabled=tokens[1].lower() == "on", client_id="stdin")
    return HELP


class ConsoleView:
    """ヘッドレス用の表示。スナップショットを受け取り、変化だけを標準出力に書く。"""

    def __init__(self, out=None) -> None:
        self._out = out or sys.stdout
        self.started_at: str | None = None  # 対局が始まった時刻（棋譜の $START_TIME 用）
        self._ply = 0
        self._modes: dict[Color, Mode | None] = {}
        self._phase: Phase | None = None

    def _print(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def on_notice(self, text: str) -> None:
        self._print(f"[通知] {text}")

    def on_snapshot(self, s: Snapshot) -> None:
        for record in s.moves[self._ply:]:
            mark = "▲" if record.color is Color.BLACK else "△"
            self._print(f"{record.ply:3d} {mark}{record.usi:6s} {record.actor.value}")
        self._ply = len(s.moves)
        for color, seat in s.seats.items():
            if self._modes.get(color) is not seat.mode and seat.mode is not None:
                if color in self._modes:
                    self._print(f"[モード] {'先手' if color is Color.BLACK else '後手'} → {seat.mode.value.upper()}")
                self._modes[color] = seat.mode
        if s.phase is not self._phase:
            self._phase = s.phase
            if s.phase is Phase.PLAYING and self.started_at is None:
                self.started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            if s.phase is Phase.ENDED:
                self._print(f"[終局] {s.result.value if s.result else ''} / "
                            f"{s.end_reason.value if s.end_reason else ''}（{len(s.moves)} 手）")


def game_info(core: GameCore, started_at: str) -> kifu.GameInfo:
    tcs = core.time_controls
    return kifu.GameInfo(
        game_id=None if core.game_id in (None, "local") else core.game_id,
        black_name=core.seat_name(Color.BLACK), white_name=core.seat_name(Color.WHITE),
        started_at=started_at, time_control=tcs[Color.BLACK] if tcs else None,
        start_sfen=core.start_sfen, start_moves=core.start_moves,
        result=core.result, end_reason=core.end_reason,
    )


def _resolve(base: Path, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else base / p


async def _run_game(config: Config, core: GameCore, transport, engines: list[EngineController],
                    log: EventLog, *, stdin, out, serve_ui=None, view: "ConsoleView | None" = None) -> int:
    """エンジン起動 → 対局 → 片付け。どの経路でも finally を通す。"""
    loop = asyncio.get_running_loop()
    started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    main_task = asyncio.current_task()
    installed: list[int] = []
    cleaning = False

    def on_signal() -> None:
        # 片付けが始まったら無視する（ハンドラを外すと既定動作に戻り、エンジンの quit → wait を飛ばして落ちる）。
        # 片付けの各段階には上限時間があるので、無視しても止まらなくなることはない。
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
    finished = False

    async def finish() -> None:
        """棋譜を保存し、進行役（サーバとの接続）を閉じる。終局したらすぐ行う（UI を開いたままでも）。"""
        nonlocal finished
        if finished:
            return
        finished = True
        try:
            if core.moves or core.result is not None:
                when = (view.started_at if view is not None else None) or started_at
                paths = kifu.save(_resolve(config.base_dir, config.record.dir), game_info(core, when),
                                  core.moves, formats=config.record.formats,
                                  mark_human_moves=config.record.mark_human_moves,
                                  score_pov=config.csa.comment_score_pov)
                for path in paths:
                    print(f"棋譜を保存しました: {path}", file=out, flush=True)
        except Exception as exc:  # 棋譜保存の失敗で片付けを止めない
            print(f"棋譜の保存に失敗しました: {exc}", file=out, flush=True)
        try:
            await transport.close()
        except Exception:
            pass

    try:
        try:
            await asyncio.gather(*(e.start() for e in engines))
        except EngineStartError as exc:
            print(f"エラー: {exc}", file=out, flush=True)
            return 1

        if stdin is not None:
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
                    command = parse_stdin_command(line, core.snapshot())
                    if isinstance(command, str):
                        print(command, file=out, flush=True)
                    elif command is not None:
                        core.post(command)

            try:
                reader_fd = stdin.fileno()
                loop.add_reader(reader_fd, on_stdin)
            except (OSError, ValueError, NotImplementedError):
                reader_fd = None
        if serve_ui is not None:
            ui_task = asyncio.create_task(serve_ui())

        await transport.start()
        await core.run()
        if core.game_id is None:
            code = 1  # 対局が始まる前に終わった（接続・LOGIN の失敗など）
        await finish()
        if ui_task is not None:
            print("対局が終わりました。UI は閲覧用に動いています（Ctrl-C で終了）", file=out, flush=True)
            await ui_task
    except asyncio.CancelledError:
        if core.phase is Phase.PLAYING:
            csa = core.game_id not in (None, "local")
            print("対局中に中断されました" + ("（サーバとの接続を切るので、この対局は負けになります）" if csa else ""),
                  file=out, flush=True)
        code = 130
    finally:
        cleaning = True
        if reader_fd is not None:
            try:
                loop.remove_reader(reader_fd)
            except Exception:
                pass
        if ui_task is not None and not ui_task.done():
            ui_task.cancel()
            try:
                await ui_task
            except (asyncio.CancelledError, Exception):
                pass
        await finish()
        await asyncio.gather(*(e.quit() for e in engines), return_exceptions=True)
        log.close()
        for sig in installed:
            loop.remove_signal_handler(sig)
    return code


def _new_log(config: Config, label: str, secrets: tuple[str, ...] = ()) -> EventLog:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return EventLog(_resolve(config.base_dir, config.record.log_dir) / f"{stamp}_{label}.jsonl", secrets=secrets)


async def run_local(config_path: Path, *, headless: bool = False, stdin=None, out=None) -> int:
    """ローカル対局。戻り値はプロセスの終了コード。"""
    out = out or sys.stdout
    try:
        config = load_config(config_path)
        local = config.local
        engine_cfgs = {
            color: (engine_config(config, name) if name else None)
            for color, name in ((Color.BLACK, local.black), (Color.WHITE, local.white))
        }
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=out, flush=True)
        return 1

    log = _new_log(config, "local", secrets=(config.ui.lan.token,))
    view = ConsoleView(out)
    seats: dict[Color, LocalSeat] = {}
    engines: list[EngineController] = []
    holder: dict[str, GameCore] = {}

    def emit(event) -> None:
        holder["core"].post(event)

    try:
        for color, ecfg in engine_cfgs.items():
            engine = make_engine(config, ecfg, color, emit, log) if ecfg else None
            if engine is not None:
                engines.append(engine)
            seats[color] = LocalSeat(
                color=color, name=ecfg.name if ecfg else "human", engine=engine,
                mode=Mode.AUTO if engine else Mode.MANUAL, ponder_enabled=bool(ecfg and ecfg.ponder),
            )
    except EngineStartError as exc:
        print(f"エラー: {exc}", file=out, flush=True)
        log.close()
        return 1

    referee = LocalReferee(
        black_name=seats[Color.BLACK].name, white_name=seats[Color.WHITE].name,
        time_control=TimeControl(total_ms=local.total_sec * 1000, byoyomi_ms=local.byoyomi_sec * 1000,
                                 increment_ms=local.increment_sec * 1000),
        max_moves=local.max_moves, nyugyoku_rule=NyugyokuRule(local.nyugyoku_rule), emit=emit,
    )
    on_snapshot, on_notice, on_info, serve_ui = _attach_ui(config, view, emit, holder, out, headless=headless)
    core = GameCore(seats=seats, transport=referee, policy=policy_from(config),
                    on_snapshot=on_snapshot, on_notice=on_notice, on_engine_info=on_info, log=log)
    holder["core"] = core
    if stdin is None and headless:
        stdin = sys.stdin
    return await _run_game(config, core, referee, engines, log, stdin=stdin, out=out, serve_ui=serve_ui, view=view)


def _attach_ui(config: Config, view: ConsoleView, emit, holder: dict, out, *, headless: bool):
    """(on_snapshot, on_notice, on_info, serve_ui) を返す。ヘッドレスなら UI なし。"""
    if headless:
        return view.on_snapshot, view.on_notice, None, None
    from .ui.gateway import build_gateway  # UI は使うときだけ読み込む

    gateway = build_gateway(config, post_command=emit, get_snapshot=lambda: holder["core"].snapshot(), out=out)

    def on_snapshot(snapshot: Snapshot) -> None:
        view.on_snapshot(snapshot)
        gateway.publish_snapshot(snapshot)

    def on_notice(text: str) -> None:
        view.on_notice(text)
        gateway.publish_notice(text)

    return on_snapshot, on_notice, gateway.publish_engine_info, gateway.serve


async def run_csa(config_path: Path, *, account_alias: str | None, headless: bool = False,
                  stdin=None, out=None, continuous: bool | None = None) -> int:
    """CSA サーバ接続。エンジンの準備（readyok）を済ませてから LOGIN する（SPEC §6.5）。

    対局の繰り返し・接続画面・名前ストックの編集は session.CsaSession が受け持つ（M7）。
    - `--account` あり: その名前ですぐ接続する。
    - `--account` なし・UI あり: 接続画面を開き、UI で名前を選んで接続する。
    - `--account` なし・ヘッドレス: csa.default_account で接続する。
    """
    from .accounts import AccountError, load_accounts, select_account
    from .session import CsaSession

    out = out or sys.stdout
    try:
        config = load_config(config_path)
        if config.csa.extended_mode:
            raise ConfigError("[csa] extended_mode = true（x1）は未対応です。false にしてください（SPEC §6.5）")
        ecfg = engine_config(config, config.csa.engine)
        accounts_path = _resolve(config.base_dir, config.csa.accounts_file)
        alias = account_alias
        try:
            accounts = load_accounts(accounts_path)
        except AccountError:
            if headless or account_alias or accounts_path.exists():
                raise
            accounts = []  # UI の接続画面で最初の名前を登録できるように、ファイルなしでも起動する
        if account_alias or headless:
            alias = select_account(accounts, account_alias, config.csa.default_account).alias
    except (ConfigError, AccountError) as exc:
        print(f"設定エラー: {exc}", file=out, flush=True)
        return 1
    if stdin is None and headless:
        stdin = sys.stdin
    try:
        session = CsaSession(config, ecfg, accounts=accounts, accounts_path=accounts_path, initial_alias=alias,
                             headless=headless, stdin=stdin, out=out, continuous=continuous)
    except EngineStartError as exc:
        print(f"エラー: {exc}", file=out, flush=True)
        return 1
    return await session.run()


async def run_fake_server(host: str, port: int) -> int:
    """偽 CSA サーバ（fakes.fake_csa_server.serve_forever）。"""
    from .fakes.fake_csa_server import FakeServerConfig, serve_forever

    await serve_forever(FakeServerConfig(host=host, port=port))
    return 0
