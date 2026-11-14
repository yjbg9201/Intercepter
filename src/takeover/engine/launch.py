"""エンジンの起動コマンドの組み立て（Linux バイナリ / Windows .exe）。

参照: SPEC §6.3「起動」、§11、§12（Windows 側のプロセス残り）

パス変換は純関数（ファイルシステムや wslpath に触る処理と分けてある）。
- 受け付ける path: Linux パス（`~` 展開あり）、`/mnt/c/...`、`C:\\...` / `C:/...`
- Windows 形式は `/mnt/<drive>/...` に変換。wslpath があればそちらを優先
  （/etc/wsl.conf の automount.root が既定と違う環境があるため）
- cwd 省略時は path のあるフォルダ
- kind = auto は拡張子（.exe）とパスの形で判定
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from .base import EngineStartError


class EngineKind(str, Enum):
    AUTO = "auto"
    LINUX = "linux"
    WINDOWS = "windows"


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """子プロセスの起動に必要なものすべて。EngineController はこれだけを見る。"""

    argv: tuple[str, ...]
    cwd: str | None
    kind: EngineKind  # AUTO は解決済みなので入らない（LINUX か WINDOWS）
    display_name: str  # ログ・UI 用（設定上のエンジン名）


_WIN_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_path(path: str) -> bool:
    """`C:\\foo` / `C:/foo` の形なら True（純関数）。"""
    return bool(_WIN_RE.match(path))


def windows_to_wsl_path(path: str, automount_root: str = "/mnt") -> PurePosixPath:
    """`C:\\shogi\\a.exe` → `/mnt/c/shogi/a.exe`（純関数。wslpath を使わない自前変換）。"""
    if not is_windows_path(path):
        raise ValueError(f"Windows 形式のパスではありません: {path!r}")
    drive = path[0].lower()
    rest = [part for part in re.split(r"[\\/]+", path[3:]) if part]
    return PurePosixPath(automount_root, drive, *rest)


def detect_kind(path: str) -> EngineKind:
    """kind = auto の判定（純関数）。LINUX か WINDOWS を返す。"""
    if is_windows_path(path) or path.lower().endswith(".exe"):
        return EngineKind.WINDOWS
    return EngineKind.LINUX


def is_wsl() -> bool:
    try:
        return "microsoft" in os.uname().release.lower() or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
    except OSError:
        return False


def _to_local_path(path: str) -> str:
    """設定に書かれたパスを、このマシン（WSL / Linux）で開けるパスにする。"""
    if is_windows_path(path):
        wslpath = shutil.which("wslpath")
        if wslpath:
            try:
                out = subprocess.run([wslpath, "-u", path], capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return str(windows_to_wsl_path(path))
    return os.path.expanduser(path)


def resolve_launch(
    *,
    name: str,
    path: str,
    cwd: str | None,
    kind: EngineKind | str = EngineKind.AUTO,
    args: tuple[str, ...] = (),
    check: bool = True,
) -> LaunchSpec:
    """設定値から LaunchSpec を作る。ここだけがファイルシステムと wslpath に触る。"""
    kind = EngineKind(kind)
    resolved_kind = detect_kind(path) if kind is EngineKind.AUTO else kind
    local = _to_local_path(path)
    local_cwd = _to_local_path(cwd) if cwd else str(Path(local).parent)

    if check:
        if resolved_kind is EngineKind.WINDOWS and not is_wsl():
            raise EngineStartError(
                f"エンジン {name!r}: Windows の .exe（{path}）は WSL 上でしか起動できません"
            )
        if not os.path.isfile(local):
            raise EngineStartError(f"エンジン {name!r}: 実行ファイルが見つかりません: {local}（設定値: {path}）")
        if resolved_kind is EngineKind.LINUX and not os.access(local, os.X_OK):
            raise EngineStartError(f"エンジン {name!r}: 実行権限がありません: {local}（chmod +x が必要）")
        if not os.path.isdir(local_cwd):
            raise EngineStartError(f"エンジン {name!r}: cwd のフォルダがありません: {local_cwd}")

    return LaunchSpec(argv=(local, *args), cwd=local_cwd, kind=resolved_kind, display_name=name)


async def ensure_windows_process_gone(spec: LaunchSpec, pid_hint: int | None) -> None:
    """Windows .exe が Windows 側に残っていないか確認し、残っていれば片付ける。

    方法は SPEC §12 の未決事項（taskkill.exe など。同名の別プロセスを巻き込まないこと）。
    決まるまでは何もしない。quit() は stdin を閉じてから kill するので、EOF で終了する
    エンジンであれば残らない。
    """
    return None
