"""エンジンの起動コマンドの組み立て（Linux バイナリ / Windows .exe）。

担当: M1（テスト: tests/test_launch.py。§9 シナリオ 15）
参照: SPEC §6.3「起動」、§11、§12（Windows 側のプロセス残り）

パス変換は純関数にする（ファイルシステムや wslpath に触る処理と分ける）。
- 受け付ける path: Linux パス（`~` 展開あり）、`/mnt/c/...`、`C:\\...` / `C:/...`
- Windows 形式は `/mnt/<drive>/...` に変換。wslpath があればそちらを優先
  （/etc/wsl.conf の automount.root が既定と違う環境があるため）
- cwd 省略時は path のあるフォルダ
- kind = auto は拡張子（.exe）とパスの形で判定
- 起動前の確認（存在・実行権限・Windows 指定なのに WSL でない）は、分かりやすい
  メッセージの EngineStartError にする
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath


class EngineKind(str, Enum):
    AUTO = "auto"
    LINUX = "linux"
    WINDOWS = "windows"


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """子プロセスの起動に必要なものすべて。EngineController はこれだけを見る。"""

    argv: tuple[str, ...]
    cwd: str
    kind: EngineKind  # AUTO は解決済みなので入らない（LINUX か WINDOWS）
    display_name: str  # ログ・UI 用（設定上のエンジン名）


def is_windows_path(path: str) -> bool:
    """`C:\\foo` / `C:/foo` の形なら True（純関数）。"""
    raise NotImplementedError("M1")


def windows_to_wsl_path(path: str, automount_root: str = "/mnt") -> PurePosixPath:
    """`C:\\shogi\\a.exe` → `/mnt/c/shogi/a.exe`（純関数。wslpath を使わない自前変換）。"""
    raise NotImplementedError("M1")


def detect_kind(path: str) -> EngineKind:
    """kind = auto の判定（純関数）。LINUX か WINDOWS を返す。"""
    raise NotImplementedError("M1")


def resolve_launch(
    *,
    name: str,
    path: str,
    cwd: str | None,
    kind: EngineKind,
    args: tuple[str, ...] = (),
) -> LaunchSpec:
    """設定値から LaunchSpec を作る。ここだけがファイルシステムと wslpath に触る。"""
    raise NotImplementedError("M1")


async def ensure_windows_process_gone(spec: LaunchSpec, pid_hint: int | None) -> None:
    """Windows .exe が Windows 側に残っていないか確認し、残っていれば片付ける。

    方法は SPEC §12 の未決事項（taskkill.exe など。同名の別プロセスを巻き込まないこと）。
    決まるまでは何もしない実装でよい。
    """
    raise NotImplementedError("M1（SPEC §12 の未決事項が決まってから）")
