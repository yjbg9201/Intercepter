"""UI へ配る読み取り専用スナップショット（SPEC §6.7）。ここは定義済み（骨組みの契約）。

GameCore だけが作り、UiGateway は JSON にして全接続へ配るだけ（I1, I8）。
秘密情報（trip / password / UI トークン）を入れる項目は作らない。
JSON への変換は ui/messages.py。
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Color, EndReason, GameResult, Mode, MoveRecord, Phase, SeatKind


@dataclass(frozen=True, slots=True)
class SeatView:
    color: Color
    kind: SeatKind
    name: str
    mode: Mode | None  # RemoteSeat は None
    has_engine: bool  # 人間専用席は False（返却できない）
    engine_state: str | None  # EngineState の値。表示用


@dataclass(frozen=True, slots=True)
class ClockView:
    """時計の表示用の値。UI は running 側を received 時刻からの経過で自前カウントダウンする。"""

    remaining_ms: dict[Color, int]  # 手番開始時点の残り
    byoyomi_ms: int
    increment_ms: int
    running: Color | None  # 時計が動いている側
    turn_elapsed_ms: int  # スナップショット作成時点での、本手番の既経過


@dataclass(frozen=True, slots=True)
class Snapshot:
    rev: int  # スナップショットの通し番号（単調増加）
    phase: Phase
    game_id: str | None
    ply: int  # これまでに指された手数。コマンドの ply と照合する
    sfen: str
    turn: Color
    my_color: Color | None  # CSA モードの自分の色。ローカルモードは None
    seats: dict[Color, SeatView]
    clock: ClockView | None
    moves: tuple[MoveRecord, ...]
    last_move: str | None
    legal_moves: tuple[str, ...]  # 人間が指せるときだけ入れる。それ以外は空
    can_declare: bool  # 手番側が MANUAL で、宣言条件を満たすとき True
    hint_enabled: bool
    start_sfen: str | None = None  # None は平手。指し手の表記を作るのに使う
    start_moves: tuple[str, ...] = ()
    result: GameResult | None = None
    end_reason: EndReason | None = None
    hint_multipv: int = 1  # いま有効なヒントの MultiPV（オフなら 1）
    hint_multipv_max: int = 1  # 設定値 hint.multipv。1 なら切替の意味がない
    declare_result: str | None = None  # can_declare のとき、宣言の結果（"win" / "draw"。draw は 24 点法のみ）
