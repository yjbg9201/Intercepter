"""名前ストック（accounts.toml）の読み書き（SPEC §6.10）。

担当: M7 で UI からの追加・編集・削除を足す

- trip / password は秘密。repr / ログ / スナップショット / UI に出さない
  （Account の repr から外してある。UI へは public_view() だけを渡す）。
- LOGIN 名は英数字と `_` `-` のみ・32 文字以内（読込時に検証）。
- password があれば game,trip より優先。どちらもなければエラー。
- alias の重複はエラー。
"""

from __future__ import annotations

import re
import tomllib
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
        if self.password:
            return self.password
        return f"{self.game},{self.trip}"

    def secrets(self) -> tuple[str, ...]:
        """ログで伏せるべき文字列（EventLog に渡す）。"""
        return tuple(s for s in (self.trip, self.password) if s)

    def public_view(self) -> dict[str, str]:
        """UI に見せてよい項目だけ（alias / name / game / memo）。"""
        return {"alias": self.alias, "name": self.name, "game": self.game or "", "memo": self.memo}


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_KEYS = {"alias", "name", "game", "trip", "password", "memo"}


def parse_accounts(data: dict) -> list[Account]:
    """tomllib が返した dict を検証して Account の列にする（純関数）。"""
    entries = data.get("account", [])
    if not isinstance(entries, list):
        raise AccountError("[[account]] の形で書いてください")
    accounts: list[Account] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries, 1):
        where = f"[[account]] {index} 件目"
        if not isinstance(raw, dict):
            raise AccountError(f"{where}: テーブルではありません")
        unknown = set(raw) - _KEYS
        if unknown:
            raise AccountError(f"{where}: 未知のキー {sorted(unknown)}")
        for key, value in raw.items():
            if not isinstance(value, str):
                raise AccountError(f"{where}: {key} は文字列で書いてください")
        alias, name = raw.get("alias"), raw.get("name")
        if not alias or not name:
            raise AccountError(f"{where}: alias と name は必須です")
        where = f"[[account]] {alias!r}"
        if alias in seen:
            raise AccountError(f"{where}: alias が重複しています")
        seen.add(alias)
        if not _NAME_RE.match(name):
            raise AccountError(f"{where}: name は英数字と _ - のみ・32 文字以内です")
        if not raw.get("password") and not (raw.get("game") and raw.get("trip")):
            raise AccountError(f"{where}: password か、game と trip の両方が必要です")
        for key in ("game", "trip", "password"):
            if raw.get(key) and re.search(r"\s", raw[key]):
                raise AccountError(f"{where}: {key} に空白は使えません")
        accounts.append(Account(alias=alias, name=name, game=raw.get("game"), trip=raw.get("trip"),
                                password=raw.get("password"), memo=raw.get("memo", "")))
    return accounts


def load_accounts(path: Path) -> list[Account]:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise AccountError(
            f"名前ストック {path} がありません（accounts.example.toml をコピーして作ってください）"
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise AccountError(f"{path}: TOML の書式エラー: {exc}") from None
    return parse_accounts(data)


def select_account(accounts: list[Account], alias: str | None, default_alias: str | None) -> Account:
    """--account の指定、なければ default_account。見つからなければ候補の alias を添えてエラー。"""
    wanted = alias or default_alias
    candidates = ", ".join(a.alias for a in accounts) or "（登録なし）"
    if not wanted:
        raise AccountError(f"--account か csa.default_account で名前を選んでください（候補: {candidates}）")
    for account in accounts:
        if account.alias == wanted:
            return account
    raise AccountError(f"名前 {wanted!r} が名前ストックにありません（候補: {candidates}）")


def save_accounts(path: Path, accounts: list[Account]) -> None:
    """v1（UI からの編集）。標準ライブラリに TOML の書き出しはないので、最小の書き出しを自前で持つ。"""
    raise NotImplementedError("M7")
