"""全モジュール共通の列挙と小さな値型。ここは定義済み（骨組みの契約）。

時間は内部ではすべて ms の int（CLAUDE.md）。指し手は内部では USI 文字列（例 "7g7f", "P*5e"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Color(str, Enum):
    BLACK = "b"  # 先手
    WHITE = "w"  # 後手

    @property
    def opponent(self) -> "Color":
        return Color.WHITE if self is Color.BLACK else Color.BLACK


class Mode(str, Enum):
    """LocalSeat の操作モード（SPEC §2）。"""

    AUTO = "auto"  # エンジンが指す
    MANUAL = "manual"  # 人間が指す


class Phase(str, Enum):
    """GameCore のフェーズ（SPEC §6.1）。ローカルモードは READY から始まる。"""

    CONNECTING = "connecting"
    WAITING_GAME = "waiting_game"
    READY = "ready"
    PLAYING = "playing"
    ENDED = "ended"


class Actor(str, Enum):
    """その手を誰が指したか。棋譜・イベントログに残す（SPEC §6.1, §6.8）。"""

    ENGINE = "engine"
    HUMAN = "human"
    REMOTE = "remote"


class SeatKind(str, Enum):
    LOCAL = "local"  # 奪取・返却できる席
    REMOTE = "remote"  # CSA の相手


class GameResult(str, Enum):
    BLACK_WIN = "black_win"
    WHITE_WIN = "white_win"
    DRAW = "draw"
    ABORTED = "aborted"  # #CHUDAN、切断、#CENSORED など


class EndReason(str, Enum):
    """終局理由。CSA の理由行（SPEC §6.5）と LocalReferee の判定（SPEC §6.6）で共通に使う。"""

    RESIGN = "resign"
    TIME_UP = "time_up"
    ILLEGAL_MOVE = "illegal_move"
    SENNICHITE = "sennichite"
    OUTE_SENNICHITE = "oute_sennichite"
    JISHOGI = "jishogi"  # 入玉宣言の成立
    ILLEGAL_DECLARATION = "illegal_declaration"  # 入玉宣言の不成立（宣言側の負け）
    MAX_MOVES = "max_moves"
    MATE = "mate"  # 詰み（ローカルモード。合法手なし）
    CHUDAN = "chudan"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class NyugyokuRule(str, Enum):
    LAW_27 = "27"
    LAW_24 = "24"  # v1


@dataclass(frozen=True, slots=True)
class Score:
    """エンジンの評価値。常に「探索時の手番側」から見た値（USI のまま）で持つ。

    表示やコメント送信のための視点変換は、使う側（ui.messages / transport.csa_parse）で行う。
    """

    cp: int | None = None  # センチポーン
    mate: int | None = None  # 詰みまでの手数。正なら手番側の勝ち
    bound: str | None = None  # "lower" / "upper" / None（暫定値の区別。SPEC §6.3）


@dataclass(frozen=True, slots=True)
class TimeControl:
    """持ち時間の設定（ms）。CSA モードは Game_Summary から、ローカルモードは設定から作る。"""

    total_ms: int
    byoyomi_ms: int = 0
    increment_ms: int = 0
    least_time_per_move_ms: int = 0
    time_unit_ms: int = 1000  # サーバの T の単位。消費時間はこの単位に切り捨てられる


@dataclass(frozen=True, slots=True)
class MoveRecord:
    """指し手履歴の 1 手（SPEC §6.1）。"""

    ply: int  # 1 始まりの通し手数
    usi: str
    color: Color
    actor: Actor
    elapsed_ms: int | None = None  # 消費時間。CSA モードではエコーの T で確定する
    score: Score | None = None  # ENGINE の手のみ
    pv: tuple[str, ...] = ()  # 読み筋（USI）。ENGINE の手のみ
