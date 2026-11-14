"""エンジンのパス解決（SPEC §6.3、§9 シナリオ 15）。"""

import os
from pathlib import PurePosixPath

import pytest

from takeover.engine import launch
from takeover.engine.base import EngineStartError
from takeover.engine.launch import EngineKind


@pytest.mark.parametrize(
    "path, expected",
    [
        (r"C:\shogi\YaneuraOu\YaneuraOu_NNUE.exe", "/mnt/c/shogi/YaneuraOu/YaneuraOu_NNUE.exe"),
        ("D:/engines/a b/e.exe", "/mnt/d/engines/a b/e.exe"),
        (r"c:\x\\y\e.exe", "/mnt/c/x/y/e.exe"),
    ],
)
def test_windows_to_wsl(path, expected):
    assert launch.is_windows_path(path)
    assert launch.windows_to_wsl_path(path) == PurePosixPath(expected)


def test_custom_automount_root():
    assert str(launch.windows_to_wsl_path(r"C:\a\b.exe", "/win")) == "/win/c/a/b.exe"


@pytest.mark.parametrize("path", ["/mnt/c/shogi/e.exe", "~/engines/e", "engines/e", "/c:/x"])
def test_not_windows_path(path):
    assert not launch.is_windows_path(path)
    with pytest.raises(ValueError):
        launch.windows_to_wsl_path(path)


def test_detect_kind():
    assert launch.detect_kind(r"C:\a\e.exe") is EngineKind.WINDOWS
    assert launch.detect_kind("/mnt/c/a/E.EXE") is EngineKind.WINDOWS
    assert launch.detect_kind("~/engines/yaneuraou") is EngineKind.LINUX


def test_resolve_linux_engine(tmp_path):
    exe = tmp_path / "engine"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    spec = launch.resolve_launch(name="main", path=str(exe), cwd=None, args=("--x",))
    assert spec.argv == (str(exe), "--x") and spec.cwd == str(tmp_path)
    assert spec.kind is EngineKind.LINUX and spec.display_name == "main"


def test_resolve_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    exe = tmp_path / "e"
    exe.write_text("")
    exe.chmod(0o755)
    assert launch.resolve_launch(name="m", path="~/e", cwd=None).argv[0] == str(exe)


def test_resolve_errors_are_clear(tmp_path):
    with pytest.raises(EngineStartError, match="見つかりません"):
        launch.resolve_launch(name="m", path=str(tmp_path / "nope"), cwd=None)
    exe = tmp_path / "e"
    exe.write_text("")
    exe.chmod(0o644)
    with pytest.raises(EngineStartError, match="実行権限"):
        launch.resolve_launch(name="m", path=str(exe), cwd=None)
    exe.chmod(0o755)
    with pytest.raises(EngineStartError, match="cwd"):
        launch.resolve_launch(name="m", path=str(exe), cwd=str(tmp_path / "nodir"))


def test_windows_exe_outside_wsl_is_rejected(monkeypatch):
    monkeypatch.setattr(launch, "is_wsl", lambda: False)
    with pytest.raises(EngineStartError, match="WSL"):
        launch.resolve_launch(name="m", path=r"C:\a\e.exe", cwd=None)


def test_windows_path_resolution_without_check(monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: None)  # wslpath なし → 自前変換
    spec = launch.resolve_launch(name="m", path=r"C:\shogi\e.exe", cwd=r"C:\shogi\eval", check=False)
    assert spec.argv == ("/mnt/c/shogi/e.exe",) and spec.cwd == "/mnt/c/shogi/eval"
    assert spec.kind is EngineKind.WINDOWS
    spec = launch.resolve_launch(name="m", path="/mnt/c/shogi/e.exe", cwd=None, check=False)
    assert spec.cwd == "/mnt/c/shogi" and spec.kind is EngineKind.WINDOWS
    assert os.path.isabs(spec.argv[0])
