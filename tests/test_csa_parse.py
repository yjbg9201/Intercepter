import cshogi
import pytest

from takeover.core.types import Color, EndReason, Score
from takeover.transport import csa_parse as c

SUMMARY = """BEGIN Game_Summary
Protocol_Version:1.2
Protocol_Mode:Server
Format:Shogi 1.0
Declaration:Jishogi 1.1
Game_ID:wdoor+floodgate-300-10F+a+b+20261114120000
Name+:a
Name-:b
Your_Turn:-
Rematch_On_Draw:NO
To_Move:+
Max_Moves:256
Some_New_Key:1
BEGIN Time
Time_Unit:1sec
Total_Time:300
Byoyomi:0
Increment:10
Least_Time_Per_Move:0
END Time
BEGIN Position
P1-KY-KE-GI-KI-OU-KI-GI-KE-KY
P2 * -HI *  *  *  *  * -KA * 
P3-FU-FU-FU-FU-FU-FU-FU-FU-FU
P4 *  *  *  *  *  *  *  *  * 
P5 *  *  *  *  *  *  *  *  * 
P6 *  *  *  *  *  *  *  *  * 
P7+FU+FU+FU+FU+FU+FU+FU+FU+FU
P8 * +KA *  *  *  *  * +HI * 
P9+KY+KE+GI+KI+OU+KI+GI+KE+KY
P+
P-
+
END Position
END Game_Summary
""".splitlines()


def test_game_summary_hirate():
    s = c.parse_game_summary(SUMMARY)
    assert s.game_id.startswith("wdoor+floodgate")
    assert (s.black_name, s.white_name, s.my_color, s.to_move) == ("a", "b", Color.WHITE, Color.BLACK)
    tc = s.time_control[Color.BLACK]
    assert (tc.total_ms, tc.byoyomi_ms, tc.increment_ms, tc.time_unit_ms) == (300000, 0, 10000, 1000)
    assert s.start_sfen is None and s.start_moves == ()
    assert s.max_moves == 256
    assert s.unknown == {"Some_New_Key": "1"}


def test_game_summary_with_moves_and_split_time():
    lines = list(SUMMARY)
    i = lines.index("END Position")
    lines[i:i] = ["+7776FU,T0", "-3334FU,T0"]
    t0, t1 = lines.index("BEGIN Time"), lines.index("END Time")
    lines[t0 : t1 + 1] = [
        "BEGIN Time+", "Time_Unit:1min", "Total_Time:10", "Byoyomi:1", "END Time+",
        "BEGIN Time-", "Time_Unit:1msec", "Total_Time:5000", "END Time-",
    ]
    s = c.parse_game_summary(lines)
    assert s.start_moves == ("7g7f", "3c3d")
    assert s.time_control[Color.BLACK].total_ms == 600000
    assert s.time_control[Color.BLACK].byoyomi_ms == 60000
    assert s.time_control[Color.WHITE].total_ms == 5000


def test_game_summary_missing_key():
    with pytest.raises(c.CsaParseError):
        c.parse_game_summary(["BEGIN Game_Summary", "Game_ID:x", "END Game_Summary"])


def test_login_and_mask():
    line = c.build_login("me", c.build_floodgate_password("floodgate-300-10F", "secret"))
    assert line == "LOGIN me floodgate-300-10F,secret"
    assert c.build_login("me", "pw", x1=True) == "LOGIN me pw x1"
    assert c.mask_login(line) == "LOGIN me ***"
    assert c.mask_login("LOGIN me pw x1") == "LOGIN me *** x1"
    assert c.mask_login("+7776FU") == "+7776FU"


def test_server_move():
    m = c.parse_server_move("+7776FU,T12")
    assert (m.color, m.csa, m.elapsed_units, m.comment) == (Color.BLACK, "+7776FU", 12, None)
    m = c.parse_server_move("-3334FU,T0,'* -30 +2726FU")
    assert (m.color, m.elapsed_units, m.comment) == (Color.WHITE, 0, "'* -30 +2726FU")
    assert c.parse_server_move("%TORYO,T3").csa == "%TORYO"
    assert c.parse_server_move("#WIN") is None
    assert c.parse_server_move("") is None
    assert c.parse_server_move("START:abc") is None


def test_end_lines():
    assert c.parse_end_line("#RESIGN").reason is EndReason.RESIGN
    assert c.parse_end_line("#OUTE_SENNICHITE").reason is EndReason.OUTE_SENNICHITE
    assert c.parse_end_line("#WIN").result == "win"
    assert c.parse_end_line("#CENSORED").result == "censored"
    assert c.parse_end_line("#SOMETHING_NEW").reason is EndReason.UNKNOWN
    assert c.parse_end_line("+7776FU,T1") is None


def test_move_conversion():
    board = cshogi.Board()
    assert c.usi_to_csa(board, "7g7f") == "+7776FU"
    assert c.csa_to_usi(board, "+7776FU") == "7g7f"
    board.push_usi("7g7f")
    assert c.usi_to_csa(board, "3c3d") == "-3334FU"
    promo = cshogi.Board(sfen="4k4/9/9/9/9/9/9/1B7/4K4 b P 1")
    assert c.usi_to_csa(promo, "8h2b+") == "+8822UM"
    assert c.usi_to_csa(promo, "P*5e") == "+0055FU"
    assert c.csa_to_usi(promo, "+8822UM") == "8h2b+"


def test_pv_to_csa_stops_at_illegal():
    board = cshogi.Board()
    assert c.pv_to_csa(board, ("7g7f", "3c3d", "9a9b", "2g2f")) == ("+7776FU", "-3334FU")
    assert board.move_number == 1  # board は変更されない


def test_comment_score_sign_convention():
    # mover 視点: USI の値をそのまま
    assert c.comment_score(Score(cp=120), Color.WHITE, "mover") == 120
    assert c.comment_score(Score(cp=120), Color.BLACK, "mover") == 120
    # 先手視点: 後手の手は反転
    assert c.comment_score(Score(cp=120), Color.WHITE, "black") == -120
    assert c.comment_score(Score(cp=120), Color.BLACK, "black") == 120
    assert c.comment_score(Score(mate=3), Color.BLACK, "mover") == c.MATE_SCORE - 3
    assert c.comment_score(Score(mate=-3), Color.BLACK, "mover") == -(c.MATE_SCORE - 3)
    assert c.comment_score(Score(), Color.BLACK, "mover") is None
    assert c.comment_to_mover_score(-120, Color.WHITE, "black") == 120


def test_move_line():
    assert c.build_move_line("+7776FU") == "+7776FU"  # 人間の手: コメントなし
    assert c.build_move_line("+7776FU", score=30, pv_csa=("-3334FU", "+2726FU")) == (
        "+7776FU,'* 30 -3334FU +2726FU")
    assert c.parse_comment("'* 30 -3334FU +2726FU") == (30, ("-3334FU", "+2726FU"))
    assert c.parse_comment("'hello") is None
