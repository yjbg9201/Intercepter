"""設定ファイル（config.toml）の型と読込（標準の tomllib）。

参照: SPEC §8（項目の一覧と既定値）。項目を足したら SPEC §8 と config.example.toml も直す。

読込時に検証すること（違反は ConfigError。どの項目がなぜ駄目かを日本語で）:
- 未知のセクション・キー（打ち間違いの検出）
- [local] / [csa] が参照するエンジン名が [engine.*] にあること
- ui.lan.enabled のとき以外は host が 127.0.0.1 / localhost であること
- csa.keepalive_sec >= 30、csa.extended_mode = true は v0 では未対応
- policy / time_guard / record の列挙値
- time_guard.min_remaining_ms が秒読み・加算以上のときは警告（エラーにはしない。SPEC §12 Q5）

タイムアウト・猶予時間はすべてここを通す（コード中にハードコードしない）。
"""

from __future__ import annotations

import tomllib
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UiLanConfig:
    enabled: bool = False
    token: str = ""  # 秘密。空なら起動時に生成
    allowed_hosts: tuple[str, ...] = ()
    public_host: str = ""  # スマホから見た、この PC のアドレス（URL と QR コードに使う）。空なら自動で調べる


@dataclass(frozen=True, slots=True)
class UiConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    notify_my_turn: bool = True
    lan: UiLanConfig = field(default_factory=UiLanConfig)


@dataclass(frozen=True, slots=True)
class EngineConfig:
    name: str  # [engine.<name>] の <name>
    path: str
    cwd: str | None = None
    kind: str = "auto"  # auto / linux / windows
    ponder: bool = False
    args: tuple[str, ...] = ()
    options: dict[str, str | int | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CsaConfig:
    host: str = "wdoor.c.u-tokyo.ac.jp"
    port: int = 4081
    accounts_file: str = "accounts.toml"
    default_account: str | None = None
    engine: str = "main"
    extended_mode: bool = False
    send_engine_comment: bool = True
    comment_score_pov: str = "black"  # black（先手視点。Floodgate の慣例）/ mover（指した側視点）
    keepalive_sec: int = 60
    connect_timeout_ms: int = 10000
    agree_timeout_ms: int = 30000
    continuous: bool = False  # 終局後に自動で再 LOGIN して次の対局を待つ（UI からも切り替えられる）
    rejoin_delay_sec: int = 10  # 連続参加で、終局から再 LOGIN までの間隔


@dataclass(frozen=True, slots=True)
class LocalConfig:
    black: str | None = "main"  # エンジン名。None（設定では "human"）は人間専用席
    white: str | None = "sub"
    total_sec: int = 300
    increment_sec: int = 10
    byoyomi_sec: int = 0
    max_moves: int = 256
    nyugyoku_rule: str = "27"
    games: int = 1  # 続けて指す局数（連続対局）
    alternate_sides: bool = True  # 連続対局で 1 局ごとに先後を入れ替える
    next_game_delay_sec: int = 3  # 終局から次の局を始めるまでの間隔（終局図を見る時間）


@dataclass(frozen=True, slots=True)
class RecordConfig:
    dir: str = "records"
    log_dir: str = "logs"
    mark_human_moves: bool = True
    formats: tuple[str, ...] = ("csa",)


@dataclass(frozen=True, slots=True)
class TimeConfig:
    margin_ms: int = 1000


@dataclass(frozen=True, slots=True)
class TimeGuardConfig:
    min_remaining_ms: int = 15000  # 0 で無効
    action: str = "auto"  # auto / warn


@dataclass(frozen=True, slots=True)
class HintConfig:
    enabled: bool = False
    multipv: int = 1
    send_as_comment: bool = False  # 人間の手がヒントの候補と一致したら、その評価値・読み筋をコメントにする


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    on_engine_resign: str = "resign"  # resign / ask
    on_engine_failure: str = "restart"  # restart / manual
    stop_grace_ms: int = 3000
    usi_timeout_ms: int = 30000
    quit_timeout_ms: int = 3000
    max_restarts: int = 3
    usi_line_limit_bytes: int = 1048576
    engine_info_interval_ms: int = 200  # info をイベントにする最小間隔（multipv ごと）


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """予約切替（v1。SPEC §7.3）。"""

    first_ply: int
    last_ply: int
    mode: str


@dataclass(frozen=True, slots=True)
class Config:
    ui: UiConfig = field(default_factory=UiConfig)
    engines: dict[str, EngineConfig] = field(default_factory=dict)
    csa: CsaConfig = field(default_factory=CsaConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    record: RecordConfig = field(default_factory=RecordConfig)
    time: TimeConfig = field(default_factory=TimeConfig)
    time_guard: TimeGuardConfig = field(default_factory=TimeGuardConfig)
    hint: HintConfig = field(default_factory=HintConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    schedule: tuple[ScheduleEntry, ...] = ()
    base_dir: Path = Path(".")  # 相対パス（accounts_file、record.dir など）の基準＝設定ファイルの場所


_SECTIONS = {
    "ui": UiConfig,
    "csa": CsaConfig,
    "local": LocalConfig,
    "record": RecordConfig,
    "time": TimeConfig,
    "time_guard": TimeGuardConfig,
    "hint": HintConfig,
    "policy": PolicyConfig,
}
_ENUMS = {
    ("csa", "comment_score_pov"): {"mover", "black"},
    ("local", "nyugyoku_rule"): {"27", "24"},
    ("time_guard", "action"): {"auto", "warn"},
    ("policy", "on_engine_resign"): {"resign", "ask"},
    ("policy", "on_engine_failure"): {"restart", "manual"},
}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigWarning(UserWarning):
    pass


def _check_type(where: str, value, annotation: str):
    """dataclass の注釈（文字列）に対する最小限の型検査。"""
    ok = True
    if annotation == "bool":
        ok = isinstance(value, bool)
    elif annotation == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif annotation in ("str", "str | None"):
        ok = isinstance(value, str)
    elif annotation.startswith("tuple[str"):
        ok = isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
        value = tuple(value)
    if not ok:
        raise ConfigError(f"{where}: 型が違います（{annotation} が必要、実際は {value!r}）")
    return value


def _build(cls, section: str, data: dict, *, skip: tuple[str, ...] = ()):
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}]: テーブルではありません")
    known = {f.name: f for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key in skip:
            continue
        if key not in known:
            raise ConfigError(f"[{section}] に未知のキー {key!r} があります（打ち間違い？ 使えるのは {sorted(known)}）")
        kwargs[key] = _check_type(f"[{section}] {key}", value, str(known[key].type))
        allowed = _ENUMS.get((section, key))
        if allowed is not None and kwargs[key] not in allowed:
            raise ConfigError(f"[{section}] {key} = {value!r} は使えません（{sorted(allowed)} のどれか）")
    try:
        return cls(**kwargs)
    except TypeError as exc:  # 必須項目の不足
        raise ConfigError(f"[{section}]: {exc}") from None


def _parse_plies(text: str) -> tuple[int, int]:
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            first, last = int(a), int(b)
        else:
            first = last = int(text)
    except (ValueError, AttributeError):
        raise ConfigError(f"[[schedule]] plies = {text!r} は \"1-8\" の形で書いてください") from None
    if first < 1 or last < first:
        raise ConfigError(f"[[schedule]] plies = {text!r} の範囲が不正です")
    return first, last


def parse_config(data: dict, *, base_dir: Path) -> Config:
    """tomllib が返した dict を検証して Config にする（純関数。警告は warnings で出す）。"""
    known_top = set(_SECTIONS) | {"engine", "schedule"}
    for key in data:
        if key not in known_top:
            raise ConfigError(f"未知のセクション [{key}] があります（使えるのは {sorted(known_top)}）")

    built = {}
    for section, cls in _SECTIONS.items():
        raw = dict(data.get(section, {}))
        if section == "ui":
            lan_raw = raw.pop("lan", {})
            lan = _build(UiLanConfig, "ui.lan", lan_raw)
            ui = _build(UiConfig, "ui", raw)
            built["ui"] = UiConfig(host=ui.host, port=ui.port, notify_my_turn=ui.notify_my_turn, lan=lan)
        elif section == "local":
            for side in ("black", "white"):
                if raw.get(side) == "human":
                    raw[side] = None
            cfg = _build(LocalConfig, "local", {k: v for k, v in raw.items() if v is not None})
            built["local"] = LocalConfig(**{
                **{f.name: getattr(cfg, f.name) for f in fields(LocalConfig)},
                **{side: None for side in ("black", "white") if side in raw and raw[side] is None},
            })
        else:
            built[section] = _build(cls, section, raw)

    engines: dict[str, EngineConfig] = {}
    engine_tables = data.get("engine", {})
    if not isinstance(engine_tables, dict):
        raise ConfigError("[engine.<名前>] の形で書いてください")
    for name, raw in engine_tables.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"[engine.{name}]: テーブルではありません")
        raw = dict(raw)
        options = raw.pop("options", {})
        if not isinstance(options, dict) or not all(isinstance(v, (str, int, bool)) for v in options.values()):
            raise ConfigError(f"[engine.{name}.options]: 値は文字列・整数・真偽値だけです")
        cfg = _build(EngineConfig, f"engine.{name}", {"name": name, **raw})
        if cfg.kind not in ("auto", "linux", "windows"):
            raise ConfigError(f"[engine.{name}] kind = {cfg.kind!r} は使えません（auto / linux / windows）")
        engines[name] = EngineConfig(name=name, path=cfg.path, cwd=cfg.cwd, kind=cfg.kind,
                                     ponder=cfg.ponder, args=cfg.args, options=dict(options))

    schedule = []
    for entry in data.get("schedule", []):
        if not isinstance(entry, dict) or set(entry) - {"plies", "mode"}:
            raise ConfigError("[[schedule]] には plies と mode だけを書いてください")
        first, last = _parse_plies(entry.get("plies"))
        if entry.get("mode") != "manual":
            raise ConfigError("[[schedule]] mode は \"manual\" だけです")
        schedule.append(ScheduleEntry(first, last, "manual"))

    config = Config(engines=engines, schedule=tuple(schedule), base_dir=base_dir, **built)
    if "local" in data:
        for side in ("black", "white"):
            name = getattr(config.local, side)
            if name is not None and name not in engines:
                raise ConfigError(f"[local] {side} = {name!r} が [engine.*] にありません（人間専用席は \"human\"）")
    if "local" in data:
        guard = config.time_guard.min_remaining_ms
        per_move = (config.local.byoyomi_sec + config.local.increment_sec) * 1000
        if guard and per_move and guard >= per_move:
            warnings.warn(
                f"[time_guard] min_remaining_ms = {guard} が 1 手あたりの秒読み・加算（{per_move} ms）以上です。"
                "持ち時間が減ると MANUAL が使えなくなります。",
                ConfigWarning,
                stacklevel=2,
            )
    if "csa" in data and config.csa.engine not in engines:
        raise ConfigError(f"[csa] engine = {config.csa.engine!r} が [engine.*] にありません")
    _validate(config)
    return config


def _validate(config: Config) -> None:
    ui, csa, local = config.ui, config.csa, config.local
    if not ui.lan.enabled and ui.host not in _LOCAL_HOSTS:
        raise ConfigError(
            f"[ui] host = {ui.host!r}: LAN に公開するには [ui.lan] enabled = true が必要です（トークン必須）"
        )
    if not (0 < ui.port < 65536):
        raise ConfigError(f"[ui] port = {ui.port} が範囲外です")
    if csa.keepalive_sec < 30:
        raise ConfigError("[csa] keepalive_sec は 30 以上にしてください（CSA プロトコルの規約）")
    if local.games < 1:
        raise ConfigError("[local] games は 1 以上にしてください")
    for format_ in config.record.formats:
        if format_ not in ("csa", "kif", "kifu"):
            raise ConfigError(f"[record] formats の {format_!r} は使えません（csa / kif / kifu）")
    for section, key in (("time", "margin_ms"), ("time_guard", "min_remaining_ms"),
                         ("policy", "stop_grace_ms"), ("policy", "usi_timeout_ms"),
                         ("policy", "quit_timeout_ms"), ("policy", "max_restarts"),
                         ("local", "total_sec"), ("local", "increment_sec"), ("local", "byoyomi_sec"),
                         ("csa", "rejoin_delay_sec"), ("local", "next_game_delay_sec")):
        if getattr(getattr(config, section), key) < 0:
            raise ConfigError(f"[{section}] {key} は 0 以上にしてください")
    if config.hint.multipv < 1:
        raise ConfigError("[hint] multipv は 1 以上にしてください")


def engine_config(config: Config, name: str) -> EngineConfig:
    try:
        return config.engines[name]
    except KeyError:
        raise ConfigError(f"エンジン {name!r} が [engine.*] にありません（あるのは {sorted(config.engines)}）") from None


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"設定ファイル {path} がありません（config.example.toml をコピーして作ってください）") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML の書式エラー: {exc}") from None
    return parse_config(data, base_dir=path.resolve().parent)
