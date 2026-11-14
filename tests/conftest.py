"""共通フィクスチャ。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    """注入用の偽の時計。advance() で進める。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms / 1000


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def fake_engine_argv(*args: str) -> tuple[str, ...]:
    """偽エンジンを子プロセスとして起動する argv。"""
    return (sys.executable, "-m", "takeover.fakes.fake_engine", *args)
