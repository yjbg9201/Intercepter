"""ソークテスト（SPEC §9）。

ローカルモードで偽エンジン（子プロセス）同士を対局させ、ランダムな時刻に
奪取／返却／人間着手／ヒントのオン・オフを注入する。乱数によるタイミングの揺らぎは
このテストの目的そのもの（決定的なレースの再現は test_game_core_races.py で行う）。

通常の `pytest -q` では少数局だけ回す。1000 局は:
    TAKEOVER_SOAK_GAMES=1000 pytest -m soak

不変条件に違反した局は、乱数シードを名前に入れたイベントログを logs/ に保存して失敗させる。
"""

import asyncio
import os
import random
import subprocess

import cshogi
import pytest

from conftest import REPO_ROOT, fake_engine_argv
from takeover.core import events as ev
from takeover.core.game import GameCore, GamePolicy
from takeover.core.seat import LocalSeat
from takeover.core.types import Color, Mode, NyugyokuRule, Phase, TimeControl
from takeover.engine.launch import EngineKind, LaunchSpec
from takeover.engine.usi import EngineController, EngineTimeouts
from takeover.record.eventlog import EventLog
from takeover.transport.local import LocalReferee

GAMES = int(os.environ.get("TAKEOVER_SOAK_GAMES", "6"))
BASE_SEED = int(os.environ.get("TAKEOVER_SOAK_SEED", "20261114"))


def engine_pids():
    out = subprocess.run(["pgrep", "-f", "takeover.fakes.fake_engine"], capture_output=True, text=True)
    return {int(p) for p in out.stdout.split()}


async def play_one(seed: int):
    """1 局指して (log, core, 違反の一覧) を返す。"""
    rng = random.Random(seed)
    log = EventLog(None)
    core_ref = {}
    emit = lambda event: core_ref["core"].post(event)  # noqa: E731
    timeouts = EngineTimeouts(usi_timeout_ms=10_000, stop_grace_ms=3_000, quit_timeout_ms=3_000,
                              max_restarts=3, line_limit_bytes=1 << 20, info_interval_ms=50)
    engines, seats = [], {}
    for color in Color:
        args = ["--seed", str(rng.randrange(10**6)), "--think-ms", str(rng.choice([0, 0, 2, 5, 15])),
                "--stop-delay-ms", str(rng.choice([0, 0, 3])), "--resign-after", str(rng.randrange(30, 70)),
                "--multipv", "2"]
        ponder = rng.random() < 0.7
        if ponder:
            args.append("--ponder")
        if rng.random() < 0.3:
            args.append("--crlf")
        if rng.random() < 0.2:
            args += ["--crash-rate", "0.02"]  # たまに落ちるエンジン（再起動の経路）
        spec = LaunchSpec(argv=fake_engine_argv(*args), cwd=None, kind=EngineKind.LINUX,
                          display_name=f"soak_{color.value}")
        engine = EngineController(color=color, launch=spec, options={}, ponder_enabled=ponder,
                                  timeouts=timeouts, emit=emit, log=log)
        engines.append(engine)
        seats[color] = LocalSeat(color=color, name=spec.display_name, engine=engine,
                                 mode=rng.choice([Mode.AUTO, Mode.AUTO, Mode.MANUAL]), ponder_enabled=ponder)
    referee = LocalReferee(black_name="b", white_name="w", time_control=TimeControl(0), max_moves=140,
                           nyugyoku_rule=NyugyokuRule.LAW_27, emit=emit)
    policy = GamePolicy(hint_enabled=rng.random() < 0.5, hint_multipv=rng.choice([1, 2]))
    core = GameCore(seats=seats, transport=referee, policy=policy, log=log, strict=True)
    core_ref["core"] = core

    async def inject():
        while core.phase is not Phase.ENDED:
            await asyncio.sleep(rng.choice([0, 0, 0.001, 0.003, 0.01]))
            snap = core.snapshot()
            roll = rng.random()
            color = rng.choice(list(Color))
            if snap.legal_moves and roll < 0.55:
                usi = rng.choice(snap.legal_moves)
                core.post(ev.MoveCommand(color=snap.turn, ply=snap.ply, usi=usi))
                if rng.random() < 0.15:  # 2 台目の端末から同時に別の手
                    core.post(ev.MoveCommand(color=snap.turn, ply=snap.ply, usi=rng.choice(snap.legal_moves)))
            elif roll < 0.70:
                core.post(ev.SetModeCommand(color=color, mode=Mode.MANUAL))
            elif roll < 0.92:
                core.post(ev.SetModeCommand(color=color, mode=Mode.AUTO))
            elif roll < 0.97:
                core.post(ev.SetHintCommand(enabled=rng.random() < 0.5))
            else:
                core.post(ev.MoveCommand(color=color, ply=max(0, snap.ply - 1), usi="7g7f"))  # 古い画面から

    error = None
    injector = None
    try:
        await asyncio.gather(*(e.start() for e in engines))
        await referee.start()
        injector = asyncio.create_task(inject())
        await asyncio.wait_for(core.run(), 120)
    except Exception as exc:  # 不変条件違反（EngineBusyError など）もここに来る
        error = repr(exc)
    finally:
        if injector is not None:
            injector.cancel()
            try:
                await injector
            except (asyncio.CancelledError, Exception):
                pass
        await referee.close()
        await asyncio.gather(*(e.quit() for e in engines), return_exceptions=True)
    return log, core, check_invariants(log, core, error)


def check_invariants(log, core, error):
    problems = []
    if error:
        problems.append(f"例外: {error}")
    if core.phase is not Phase.ENDED:
        problems.append("終局していない")

    # コミットした手はすべて合法／1 手番 1 手
    board = cshogi.Board()
    for record in core.moves:
        try:
            ok = board.is_legal(board.move_from_usi(record.usi))
        except Exception:
            ok = False
        if not ok:
            problems.append(f"非合法手のコミット: {record.ply} {record.usi}")
            break
        board.push_usi(record.usi)
    plies = [r["ply"] for r in log.records if r["kind"] == "commit" and "ply" in r]
    if plies != list(range(1, len(plies) + 1)):
        problems.append(f"コミットの手数が連番でない: {plies[:20]}")

    # 未完了の探索は常に 1 つ以下: エンジンごとに「go → bestmove」が交互になっている
    searching = {}
    modes = {}
    hint_ids = {c: set() for c in ("b", "w")}
    for r in log.records:
        kind, color = r["kind"], r.get("color")
        if kind == "usi_send" and r["line"].startswith("go"):
            if searching.get(color):
                problems.append(f"探索中に go を送った: {color} {r['line']}")
            searching[color] = True
        elif kind == "usi_recv" and r["line"].startswith("bestmove"):
            searching[color] = False
        elif kind == "engine_spawn":
            searching[color] = False
        elif kind == "mode_change":
            modes[color] = r["mode"]
        elif kind == "hint":
            hint_ids[color].add(r["search_id"])
        elif kind == "commit" and r.get("actor") == "engine" and modes.get(color) == "manual":
            problems.append(f"MANUAL の席でエンジンの手をコミットした: {r}")
    # ヒント探索の bestmove はコミットされない: ヒントの search_id が think のものと重ならない
    think_ids = {c: {r["search_id"] for r in log.records if r["kind"] == "think" and r.get("color") == c}
                 for c in ("b", "w")}
    for c in ("b", "w"):
        if hint_ids[c] & think_ids[c]:
            problems.append(f"ヒントと思考の search_id が重なった: {c}")
    if any(r["kind"] in ("handler_error", "engine_error", "echo_mismatch") for r in log.records):
        problems.append("エラーのログがある")
    return problems


@pytest.mark.soak
async def test_soak_random_takeovers():
    before = engine_pids()
    stats = {"games": 0, "human": 0, "engine": 0, "discarded": 0}
    for index in range(GAMES):
        seed = BASE_SEED + index
        log, core, problems = await play_one(seed)
        leftover = engine_pids() - before
        if leftover:
            problems.append(f"子プロセスが残っている: {sorted(leftover)}")
        if problems:
            path = REPO_ROOT / "logs" / f"soak_fail_seed{seed}.jsonl"
            log.dump(path)
            pytest.fail(f"seed={seed}: {problems}（ログ: {path}）")
        stats["games"] += 1
        stats["human"] += sum(1 for m in core.moves if m.actor.value == "human")
        stats["engine"] += sum(1 for m in core.moves if m.actor.value == "engine")
        stats["discarded"] += sum(1 for r in log.records if r["kind"] == "bestmove_discarded")
    # 注入が実際に効いていること（人間の手もエンジンの手も指され、破棄も起きている）
    assert stats["human"] > 0 and stats["engine"] > 0 and stats["discarded"] > 0, stats
