from takeover.core.types import Score
from takeover.engine.base import GoTime
from takeover.engine import usi_parse as u


def test_bestmove():
    assert u.parse_bestmove("bestmove 7g7f ponder 3c3d") == u.ParsedBestMove("7g7f", "3c3d")
    assert u.parse_bestmove("bestmove 7g7f") == u.ParsedBestMove("7g7f", None)
    assert u.parse_bestmove("bestmove resign").move == "resign"
    assert u.parse_bestmove("bestmove win").move == "win"
    assert u.parse_bestmove("info depth 1") is None
    assert u.parse_bestmove("bestmove") is None


def test_info_full():
    info = u.parse_info("info depth 12 seldepth 20 multipv 2 score cp -34 nodes 1000 nps 500 hashfull 3 time 20 pv 7g7f 3c3d")
    assert (info.depth, info.seldepth, info.multipv, info.nodes, info.nps, info.hashfull, info.time_ms) == (
        12, 20, 2, 1000, 500, 3, 20)
    assert info.score == Score(cp=-34)
    assert info.pv == ("7g7f", "3c3d")


def test_info_bounds_and_mate():
    assert u.parse_info("info score cp 50 lowerbound pv 7g7f").score == Score(cp=50, bound="lower")
    assert u.parse_info("info score cp 50 upperbound").score == Score(cp=50, bound="upper")
    assert u.parse_info("info score mate 5 pv 7g7f").score == Score(mate=5)
    assert u.parse_info("info score mate -3").score == Score(mate=-3)
    assert u.parse_info("info score mate +").score == Score(mate=1)


def test_info_string_and_garbage():
    assert u.parse_info("info string hello pv world").string == "hello pv world"
    assert u.parse_info("info depth x score cp y nodes") == u.ParsedInfo()
    assert u.parse_info("info currmove 7g7f depth 3").depth == 3
    assert u.parse_info("readyok") is None
    assert u.parse_info("info score") == u.ParsedInfo()


def test_id():
    assert u.parse_id("id name YaneuraOu NNUE 8.0") == ("name", "YaneuraOu NNUE 8.0")
    assert u.parse_id("usiok") is None


def test_build_position_always_full():
    assert u.build_position(None, []) == "position startpos"
    assert u.build_position(None, ["7g7f", "3c3d"]) == "position startpos moves 7g7f 3c3d"
    assert u.build_position("4k4/9/9/9/9/9/9/9/4K4 b - 1", ["5i5h"]) == (
        "position sfen 4k4/9/9/9/9/9/9/9/4K4 b - 1 moves 5i5h")


def test_build_go():
    assert u.build_go(GoTime(1000, 2000, byoyomi_ms=3000)) == "go btime 1000 wtime 2000 byoyomi 3000"
    assert u.build_go(GoTime(1000, 2000, binc_ms=10, winc_ms=20), ponder=True) == (
        "go ponder btime 1000 wtime 2000 binc 10 winc 20")
    assert u.build_go(GoTime(-5, 0)) == "go btime 0 wtime 0 byoyomi 0"


def test_setoption():
    assert u.build_setoption("USI_Ponder", True) == "setoption name USI_Ponder value true"
    assert u.build_setoptions({"Threads": 8, "EvalDir": "eval"}) == [
        "setoption name Threads value 8", "setoption name EvalDir value eval"]
