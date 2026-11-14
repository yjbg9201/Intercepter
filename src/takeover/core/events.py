"""GameCore の単一キューに入るもの全部（SPEC §6.1, §7.2）。ここは定義済み（骨組みの契約）。

入力は 3 系統で、すべて同じ asyncio.Queue に入り、到着順に 1 つずつ処理される。

- Command   : UI（または CLI の標準入力）から。必ず GameCore で検証する（I8）
- EngineEvent   : EngineController から。search_id で照合する（I5, I11）
- TransportEvent: Transport（CSA サーバ／LocalReferee）から
- TimerFired    : GameCore 自身が張ったタイマーから

項目を足すのは自由だが、意味を変えるときは SPEC を先に直す。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Color, EndReason, GameResult, Mode, Score, TimeControl

# --------------------------------------------------------------------------- #
# Command（UI → GameCore）。SPEC §6.7 の WebSocket メッセージと 1 対 1。
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Command:
    """全コマンドの基底。client_id は発行元の接続（通知・ログ用）。"""

    client_id: str = field(default="", kw_only=True)


@dataclass(frozen=True, slots=True)
class MoveCommand(Command):
    """人間の着手。ply は UI が表示していたスナップショットの手数（不一致なら拒否）。"""

    color: Color
    ply: int
    usi: str


@dataclass(frozen=True, slots=True)
class SetModeCommand(Command):
    """奪取（MANUAL）／返却（AUTO）。トグルではなく目標モードを指定する。"""

    color: Color
    mode: Mode


@dataclass(frozen=True, slots=True)
class ResignCommand(Command):
    color: Color
    ply: int


@dataclass(frozen=True, slots=True)
class DeclareCommand(Command):
    """入玉宣言。GameCore が条件成立と判定したときだけ送る（SPEC §7.4）。"""

    color: Color
    ply: int


@dataclass(frozen=True, slots=True)
class SetHintCommand(Command):
    """ヒント表示のオン／オフ（v1。SPEC §6.9）。"""

    enabled: bool


# --------------------------------------------------------------------------- #
# EngineEvent（EngineController → GameCore）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EngineEvent:
    color: Color  # どの席のエンジンか


@dataclass(frozen=True, slots=True)
class BestMoveEvent(EngineEvent):
    """bestmove。GameCore は search_id == expected_search_id のときだけ採用する（I5）。

    move は USI の指し手、または "resign" / "win"。ponder は予想手（なければ None）。
    """

    search_id: int
    move: str
    ponder: str | None = None
    score: Score | None = None  # この探索の最後の info から
    pv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineInfoEvent(EngineEvent):
    """info 行（間引き済み）。表示専用。"""

    search_id: int
    depth: int | None = None
    seldepth: int | None = None
    score: Score | None = None
    nodes: int | None = None
    nps: int | None = None
    hashfull: int | None = None
    multipv: int = 1
    pv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineReadyEvent(EngineEvent):
    """（再）起動が終わり、探索を受け付けられるようになった（SPEC §6.3）。"""

    restarted: bool = False


@dataclass(frozen=True, slots=True)
class EngineDeadEvent(EngineEvent):
    """プロセスが死んだ／再起動に失敗した／再起動上限に達した（SPEC §7.4）。"""

    reason: str = ""
    will_restart: bool = False


# --------------------------------------------------------------------------- #
# TransportEvent（Transport → GameCore）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TransportEvent:
    pass


@dataclass(frozen=True, slots=True)
class GameOffered(TransportEvent):
    """対局条件が届いた（CSA の Game_Summary）。ローカルモードは設定から作る。"""

    game_id: str
    black_name: str
    white_name: str
    my_color: Color | None  # ローカルモードは None（両席とも自分）
    time_control: TimeControl
    time_control_white: TimeControl | None = None  # 先後で持ち時間が違うときだけ（None は同じ）
    start_sfen: str | None = None  # None は平手
    start_moves: tuple[str, ...] = ()  # 開始局面からの途中手順（USI）
    max_moves: int | None = None


@dataclass(frozen=True, slots=True)
class GameStarted(TransportEvent):
    """START を受け取った。ここから時計が動く。"""

    game_id: str


@dataclass(frozen=True, slots=True)
class GameRejected(TransportEvent):
    """REJECT。WAITING_GAME に戻る（SPEC §6.5）。"""

    game_id: str
    by: str = ""


@dataclass(frozen=True, slots=True)
class MoveConfirmed(TransportEvent):
    """指し手が進行役に確定された。

    CSA モード: サーバからの `<指し手>,T<n>`。自分の手ならエコー（検証と時計の確定）、
    相手の手ならここで盤面に反映する。ローカルモード: LocalReferee が受理した手。
    """

    color: Color
    usi: str
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class GameEnded(TransportEvent):
    result: GameResult
    reason: EndReason
    detail: str = ""  # 生の理由行など


@dataclass(frozen=True, slots=True)
class TransportClosed(TransportEvent):
    """接続が切れた（CSA モード）。記録を保存して終了する。"""

    detail: str = ""


# --------------------------------------------------------------------------- #
# Timer（GameCore → GameCore）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimerFired:
    """期限タイマーの発火（SPEC §6.1）。turn_token が現在の手番と違えば無視する。

    kind: "time_guard"（TimeGuard の閾値到達） / "flag"（ローカルモードの時間切れ）
    """

    kind: str
    turn_token: int


Event = Command | EngineEvent | TransportEvent | TimerFired
