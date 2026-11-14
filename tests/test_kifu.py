"""手元の棋譜（SPEC §6.8、§9 シナリオ 12 の手元側）。"""

import cshogi.CSA

from takeover.core.types import Actor, Color, EndReason, GameResult, MoveRecord, Score, TimeControl
from takeover.record import kifu

INFO = kifu.GameInfo(
    game_id="wdoor+floodgate-300-10F+a+b+2026", black_name="a b", white_name="eng/2",
    started_at="2026-11-14T12:34:56+09:00", time_control=TimeControl(300_000, increment_ms=10_000),
    result=GameResult.WHITE_WIN, end_reason=EndReason.RESIGN)
MOVES = [
    MoveRecord(1, "7g7f", Color.BLACK, Actor.HUMAN, elapsed_ms=3000),
    MoveRecord(2, "3c3d", Color.WHITE, Actor.ENGINE, elapsed_ms=1000, score=Score(cp=-42),
               pv=("3c3d", "2g2f", "8c8d")),
    MoveRecord(3, "2g2f", Color.BLACK, Actor.REMOTE, elapsed_ms=0),
]


def test_filename_is_sanitized():
    name = kifu.kifu_filename(INFO)
    assert name == "20261114_123456_wdoor+floodgate-300-10F+a+b+2026_a_b_eng_2.csa"
    assert kifu.kifu_filename(kifu.GameInfo(None, "x", "y", "2026-11-14T01:02:03")).startswith(
        "20261114_010203_local_x_y")


def test_csa_with_human_mark_and_engine_comment():
    text = kifu.render_csa(INFO, MOVES, mark_human_moves=True)
    lines = text.splitlines()
    i = lines.index("+7776FU")
    assert lines[i : i + 3] == ["+7776FU", "T3", "'human"]
    j = lines.index("-3334FU")
    assert lines[j : j + 3] == ["-3334FU", "T1", "'** -42 +2726FU -8384FU"]
    assert "%TORYO" in lines and lines[1:3] == ["N+a b", "N-eng/2"]
    parsed = cshogi.CSA.Parser.parse_str(text)[0]  # CSA として読める
    assert len(parsed.moves) == 3


def test_human_mark_is_optional():
    assert "'human" not in kifu.render_csa(INFO, MOVES, mark_human_moves=False)


def test_save_writes_file(tmp_path):
    (path,) = kifu.save(tmp_path / "records", INFO, MOVES, formats=["csa"], mark_human_moves=True)
    assert path.exists() and path.read_text(encoding="utf-8").startswith("V2.2")
