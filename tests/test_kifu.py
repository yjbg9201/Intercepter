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
    assert lines[j : j + 3] == ["-3334FU", "T1", "'** 42 +2726FU -8384FU"]  # 先手視点（既定）
    mover = kifu.render_csa(INFO, MOVES, mark_human_moves=True, score_pov="mover").splitlines()
    assert "'** -42 +2726FU -8384FU" in mover
    assert "%TORYO" in lines and lines[1:3] == ["N+a b", "N-eng/2"]
    parsed = cshogi.CSA.Parser.parse_str(text)[0]  # CSA として読める
    assert len(parsed.moves) == 3


def test_human_move_with_hint_line_gets_the_same_comment_as_engine_moves():
    from takeover.core.types import Actor, Color, MoveRecord, Score

    moves = [MoveRecord(ply=1, usi="7g7f", color=Color.BLACK, actor=Actor.HUMAN, elapsed_ms=3000,
                        score=Score(cp=30), pv=("7g7f", "3c3d"))]
    lines = kifu.render_csa(INFO, moves, mark_human_moves=True).splitlines()
    i = lines.index("+7776FU")
    assert lines[i : i + 4] == ["+7776FU", "T3", "'** 30 -3334FU", "'human"]


def test_human_mark_is_optional():
    assert "'human" not in kifu.render_csa(INFO, MOVES, mark_human_moves=False)


def test_save_writes_file(tmp_path):
    (path,) = kifu.save(tmp_path / "records", INFO, MOVES, formats=["csa"], mark_human_moves=True)
    assert path.exists() and path.read_text(encoding="utf-8").startswith("V2.2")


# ---- KIF（M7） ------------------------------------------------------------ #

KIF_MOVES = [
    MoveRecord(1, "7g7f", Color.BLACK, Actor.HUMAN, elapsed_ms=3000),
    MoveRecord(2, "3c3d", Color.WHITE, Actor.ENGINE, elapsed_ms=61000, score=Score(cp=-42),
               pv=("3c3d", "8h2b+", "3a2b")),
    MoveRecord(3, "8h2b+", Color.BLACK, Actor.REMOTE, elapsed_ms=0),
    MoveRecord(4, "3a2b", Color.WHITE, Actor.ENGINE, elapsed_ms=2000, score=Score(mate=-3), pv=("3a2b",)),
    MoveRecord(5, "B*5e", Color.BLACK, Actor.HUMAN, elapsed_ms=1000),
]


def test_kif_moves_times_and_comments():
    lines = kifu.render_kif(INFO, KIF_MOVES, mark_human_moves=True).splitlines()
    assert lines[:6] == [
        "開始日時：2026/11/14 12:34:56", "棋戦：wdoor+floodgate-300-10F+a+b+2026",
        "持ち時間：5分+1手10秒加算", "手合割：平手", "先手：a b", "後手：eng/2"]
    body = lines[lines.index("手数----指手---------消費時間--") + 1:]
    assert body[0].split() == ["1", "７六歩(77)", "(", "0:03/00:00:03)"]
    assert body[1] == "*人間の手"
    assert body[2].split()[:2] == ["2", "３四歩(33)"] and body[2].endswith("( 1:01/00:01:01)")
    assert body[3] == "*評価値 +42 ▲２二角成 △同　銀"  # 先手視点。読み筋は指した手の次から
    assert body[4].split()[:2] == ["3", "２二角成(88)"]
    assert body[5].startswith("   4 同　銀(31)") and body[5].endswith("( 0:02/00:01:03)")  # 後手の累計
    assert body[6] == f"*評価値 {100000 - 3:+d}"  # 後手が詰まされる＝先手視点でプラス
    assert body[7].split()[:2] == ["5", "５五角打"]
    assert body[-2:] == ["   6 投了", "まで5手で後手の勝ち"]
    assert "*人間の手" not in kifu.render_kif(INFO, KIF_MOVES, mark_human_moves=False)


def test_kif_end_words():
    def end(result, reason, moves=KIF_MOVES):
        info = kifu.GameInfo(None, "x", "y", "2026-11-14T01:02:03", result=result, end_reason=reason)
        return kifu.render_kif(info, moves, mark_human_moves=False).splitlines()[-2:]

    assert end(GameResult.BLACK_WIN, EndReason.JISHOGI) == ["   6 入玉勝ち", "まで5手で先手の勝ち"]
    assert end(GameResult.DRAW, EndReason.JISHOGI) == ["   6 持将棋", "まで5手で持将棋"]
    assert end(GameResult.DRAW, EndReason.SENNICHITE) == ["   6 千日手", "まで5手で千日手"]
    assert end(GameResult.BLACK_WIN, EndReason.TIME_UP) == ["   6 切れ負け", "まで5手で先手の勝ち"]
    # 5 手指したあとは後手番。後手の勝ちなら「反則勝ち」（手番側の勝ち）、先手の勝ちなら「反則負け」
    assert end(GameResult.WHITE_WIN, EndReason.ILLEGAL_MOVE)[0] == "   6 反則勝ち"
    assert end(GameResult.BLACK_WIN, EndReason.ILLEGAL_MOVE)[0] == "   6 反則負け"
    assert end(GameResult.ABORTED, EndReason.DISCONNECTED) == ["   6 中断", "まで5手で中断"]
    assert end(None, None)[-1].split()[0] == "5"  # 途中で保存した棋譜には終局の行を書かない


def test_kif_with_start_position_and_start_moves():
    info = kifu.GameInfo(None, "x", "y", "2026-11-14T01:02:03",
                         start_sfen="4k4/9/9/9/9/9/9/9/4K4 w R2b18p 1", start_moves=("5a5b",))
    lines = kifu.render_kif(info, [MoveRecord(1, "5i5h", Color.BLACK, Actor.ENGINE)],
                            mark_human_moves=False).splitlines()
    assert "手合割：平手" not in lines
    i = lines.index("後手の持駒：角二　歩十八")
    assert lines[i + 1 : i + 4] == ["  ９ ８ ７ ６ ５ ４ ３ ２ １", "+---------------------------+",
                                    "| ・ ・ ・ ・v玉 ・ ・ ・ ・|一"]
    assert "先手の持駒：飛" in lines and "後手番" in lines
    body = lines[lines.index("手数----指手---------消費時間--") + 1:]
    assert body[0].split()[:2] == ["1", "５二玉(51)"] and body[1].split()[:2] == ["2", "５八玉(59)"]


def test_save_formats_encodings_and_no_overwrite(tmp_path):
    paths = kifu.save(tmp_path, INFO, KIF_MOVES, formats=["csa", "kif", "kifu", "bogus"], mark_human_moves=True)
    assert [p.suffix for p in paths] == [".csa", ".kif", ".kifu"]
    assert "７六歩(77)" in paths[1].read_text(encoding="cp932")  # .kif は Shift_JIS
    utf8 = paths[2].read_text(encoding="utf-8")
    assert utf8.startswith("#KIF version=2.0 encoding=UTF-8\n") and "７六歩(77)" in utf8
    (again,) = kifu.save(tmp_path, INFO, KIF_MOVES, formats=["csa"], mark_human_moves=True)
    assert again.name == paths[0].stem + "_2.csa" and paths[0].exists()  # 同じ秒・同じ対局名でも上書きしない
