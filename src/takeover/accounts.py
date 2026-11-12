"""名前ストック（accounts.toml）の読み書き（SPEC §6.10）。

担当: M0（読込と選択。テスト: tests/test_accounts.py）、M7（UI からの追加・編集・削除）

- trip / password は秘密。repr / ログ / スナップショット / UI に出さない
  （Account の repr から外してある。UI へは public_view() だけを渡す）。
- LOGIN 名は英数字と `_` `-` のみ・32 文字以内（読込時に検証）。
- password があれば game,trip より優先。どちらもなければエラー。
- alias の重複はエラー。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AccountError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Account:
    alias: str
    name: str
    game: str | None = None
    trip: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    memo: str = ""

    def login_password(self) -> str:
        """LOGIN に使うパスワード欄（password、なければ `<game>,<trip>`）。"""
        raise NotImplementedError("M0")

    def secrets(self) -> tuple[str, ...]:
        """ログで伏せるべき文字列（EventLog に渡す）。"""
        raise NotImplementedError("M0")

    def public_view(self) -> dict[str, str]:
        """UI に見せてよい項目だけ（alias / name / game / memo）。"""
        raise NotImplementedError("M0")


def parse_accounts(data: dict) -> list[Account]:
    """tomllib が返した dict を検証して Account の列にする（純関数）。"""
    raise NotImplementedError("M0")


def load_accounts(path: Path) -> list[Account]:
    raise NotImplementedError("M0")


def select_account(accounts: list[Account], alias: str | None, default_alias: str | None) -> Account:
    """--account の指定、なければ default_account。見つからなければ候補の alias を添えてエラー。"""
    raise NotImplementedError("M0")


def save_accounts(path: Path, accounts: list[Account]) -> None:
    """v1（UI からの編集）。標準ライブラリに TOML の書き出しはないので、最小の書き出しを自前で持つ。"""
    raise NotImplementedError("M7")
