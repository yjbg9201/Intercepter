"""共通フィクスチャ。M0 以降で足していく（偽の時計、偽エンジンの起動引数、偽 CSA サーバなど）。"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
