"""設定ファイル（config.toml）の型と読込（標準の tomllib）。

担当: M0（テスト: tests/test_config.py。config.example.toml がそのまま読めること）
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

from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UiLanConfig:
    enabled: bool = False
    token: str = ""  # 秘密。空なら起動時に生成
    allowed_hosts: tuple[str, ...] = ()


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
    comment_score_pov: str = "mover"  # mover / black
    keepalive_sec: int = 60
    connect_timeout_ms: int = 10000
    agree_timeout_ms: int = 30000


@dataclass(frozen=True, slots=True)
class LocalConfig:
    black: str | None = "main"  # エンジン名。None（設定では "human"）は人間専用席
    white: str | None = "sub"
    total_sec: int = 300
    increment_sec: int = 10
    byoyomi_sec: int = 0
    max_moves: int = 256
    nyugyoku_rule: str = "27"


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


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    on_engine_resign: str = "resign"  # resign / ask
    on_engine_failure: str = "restart"  # restart / manual
    stop_grace_ms: int = 3000
    usi_timeout_ms: int = 30000
    quit_timeout_ms: int = 3000
    max_restarts: int = 3
    usi_line_limit_bytes: int = 1048576


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


def parse_config(data: dict, *, base_dir: Path) -> Config:
    """tomllib が返した dict を検証して Config にする（純関数）。"""
    raise NotImplementedError("M0")


def load_config(path: Path) -> Config:
    raise NotImplementedError("M0")
