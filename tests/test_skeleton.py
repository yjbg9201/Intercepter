"""骨組みの整合性チェック。実装が進んでも緑のまま保つ（構成の約束事を守るためのテスト）。"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import tomllib
from pathlib import Path

import takeover
from takeover.core import events
from takeover.core.types import Color, Mode
from takeover.engine.base import Engine
from takeover.engine.usi import EngineController
from takeover.fakes.scripted_engine import ScriptedEngine
from takeover.transport.base import Transport
from takeover.transport.csa import CsaTransport
from takeover.transport.local import LocalReferee

SRC = Path(takeover.__file__).resolve().parent


def _all_modules() -> list[str]:
    return [m.name for m in pkgutil.walk_packages(takeover.__path__, prefix="takeover.")]


def test_all_modules_import() -> None:
    names = _all_modules()
    assert "takeover.core.game" in names
    for name in names:
        if name == "takeover.__main__":
            continue
        importlib.import_module(name)


def _imports_of(path: Path) -> set[str]:
    """ファイルが import している takeover 内のモジュール（絶対名）を返す。"""
    rel = path.relative_to(SRC.parent).with_suffix("")
    package = list(rel.parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
            elif node.module and node.module.startswith("takeover"):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("takeover"))
    return found


def test_core_depends_only_on_interfaces() -> None:
    """core が import してよいのは、インターフェース（base）と I/O を持たない純関数（usi_parse）だけ。"""
    allowed = {"takeover.engine.base", "takeover.transport.base", "takeover.engine.usi_parse"}
    for path in (SRC / "core").glob("*.py"):
        for module in _imports_of(path):
            if module.startswith("takeover.core") or module in allowed or module == "takeover":
                continue
            # `from ..engine.base import X` は "takeover.engine.base.X" としても現れる
            if any(module.startswith(a + ".") for a in allowed):
                continue
            raise AssertionError(f"{path.name} が {module} を import している（SPEC §5.5 の依存の向き）")


def test_main_code_does_not_import_fakes() -> None:
    for path in SRC.rglob("*.py"):
        if "fakes" in path.parts or path.name in {"app.py", "__main__.py"}:
            continue
        bad = [m for m in _imports_of(path) if m.startswith("takeover.fakes")]
        assert not bad, f"{path} が fakes を import している"


def test_no_threading_in_main_code() -> None:
    """I3: 本体コードは threading を使わない（別プロセスである fake_engine は対象外）。"""
    for path in SRC.rglob("*.py"):
        if path.name == "fake_engine.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert "threading" not in names, f"{path} が threading を import している"


def test_implementations_have_interface_methods() -> None:
    def methods(proto: type) -> set[str]:
        return {n for n in vars(proto) if not n.startswith("_") and callable(getattr(proto, n))}

    for impl in (EngineController, ScriptedEngine):
        missing = {m for m in methods(Engine) if not hasattr(impl, m)}
        assert not missing, f"{impl.__name__} に {missing} がない"
    for impl in (LocalReferee, CsaTransport):
        missing = {m for m in methods(Transport) if not hasattr(impl, m)}
        assert not missing, f"{impl.__name__} に {missing} がない"


def test_events_are_constructible() -> None:
    cmd = events.MoveCommand(color=Color.BLACK, ply=0, usi="7g7f", client_id="c1")
    assert cmd.client_id == "c1"
    assert events.SetModeCommand(color=Color.WHITE, mode=Mode.MANUAL).mode is Mode.MANUAL
    best = events.BestMoveEvent(color=Color.BLACK, search_id=3, move="7g7f", ponder="3c3d")
    assert (best.move, best.ponder) == ("7g7f", "3c3d")
    assert Color.BLACK.opponent is Color.WHITE


def test_example_files_parse(repo_root: Path) -> None:
    config = tomllib.loads((repo_root / "config.example.toml").read_text(encoding="utf-8"))
    assert {"ui", "engine", "csa", "local", "record", "time", "time_guard", "hint", "policy"} <= set(config)
    assert config["csa"]["extended_mode"] is False
    assert config["csa"]["keepalive_sec"] >= 30
    accounts = tomllib.loads((repo_root / "accounts.example.toml").read_text(encoding="utf-8"))
    assert len(accounts["account"]) >= 2


def test_gitignore_covers_secrets(repo_root: Path) -> None:
    lines = (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for required in ("config.toml", "accounts.toml", "logs/", "records/"):
        assert required in lines
