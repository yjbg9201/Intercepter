"""名前ストック（accounts.toml）の読み書き（SPEC §6.10）。

UI からの追加・編集・削除（M7）は upsert_account / delete_account で新しい列を作り、save_accounts で書き戻す。
最後に使った alias は別ファイル（accounts.toml の隣の `<名前>.state.json`）に置く（秘密は入れない）。

- trip / password は秘密。repr / ログ / スナップショット / UI に出さない
  （Account の repr から外してある。UI へは public_view() だけを渡す）。
- LOGIN 名は英数字と `_` `-` のみ・32 文字以内（読込時に検証）。
- password があれば game,trip より優先。どちらもなければエラー。
- alias の重複はエラー。
"""

from __future__ import annotations

import json
import os
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


def find_account(accounts: list[Account], alias: str | None) -> Account | None:
    for account in accounts:
        if account.alias == alias:
            return account
    return None


def _to_dict(account: Account) -> dict[str, str]:
    data = {"alias": account.alias, "name": account.name}
    for key in ("game", "trip", "password", "memo"):
        value = getattr(account, key)
        if value:
            data[key] = value
    return data


def upsert_account(accounts: list[Account], *, alias: str, name: str, game: str = "", trip: str = "",
                   password: str = "", memo: str = "", original_alias: str | None = None) -> list[Account]:
    """追加（original_alias なし）または編集した新しい列を返す（純関数。元の列は変えない）。

    編集で trip / password が空なら元の値を保つ（UI には秘密を出さないので、空は「変更なし」の意味）。
    検証は parse_accounts と同じ規則。違反は AccountError。
    """
    entries = [_to_dict(a) for a in accounts]
    new = {"alias": alias, "name": name}
    for key, value in (("game", game), ("trip", trip), ("password", password), ("memo", memo)):
        if value:
            new[key] = value
    if original_alias is None:
        entries.append(new)
    else:
        for index, entry in enumerate(entries):
            if entry["alias"] == original_alias:
                for key in ("trip", "password"):
                    if key not in new and key in entry:
                        new[key] = entry[key]
                entries[index] = new
                break
        else:
            raise AccountError(f"名前 {original_alias!r} が名前ストックにありません")
    return parse_accounts({"account": entries})


def delete_account(accounts: list[Account], alias: str) -> list[Account]:
    if find_account(accounts, alias) is None:
        raise AccountError(f"名前 {alias!r} が名前ストックにありません")
    return [a for a in accounts if a.alias != alias]


def render_accounts(accounts: list[Account]) -> str:
    """accounts.toml の文字列（純関数）。文字列は JSON と同じ書き方で書く（TOML の基本文字列として正しい）。"""
    lines = ["# accounts.toml（名前ストック）。trip / password は秘密情報（git 管理外。docs/SPEC.md §6.10）。",
             "# UI の接続画面から編集すると、このファイルは書き直されます（手書きのコメントは残りません）。"]
    for account in accounts:
        lines += ["", "[[account]]"]
        lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in _to_dict(account).items()]
    return "\n".join(lines) + "\n"


def save_accounts(path: Path, accounts: list[Account]) -> None:
    """名前ストックを書き戻す。一時ファイルに書いてから置き換える（途中で落ちても壊さない）。権限は 600。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(render_accounts(accounts))
    os.replace(tmp, path)


def state_path(accounts_path: Path) -> Path:
    accounts_path = Path(accounts_path)
    return accounts_path.with_name(accounts_path.stem + ".state.json")


def load_last_alias(accounts_path: Path) -> str | None:
    """最後に使った alias（なければ None）。壊れたファイルは無視する。"""
    try:
        data = json.loads(state_path(accounts_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    alias = data.get("last_alias") if isinstance(data, dict) else None
    return alias if isinstance(alias, str) and alias else None


def save_last_alias(accounts_path: Path, alias: str) -> None:
    try:
        state_path(accounts_path).write_text(json.dumps({"last_alias": alias}, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
    except OSError:
        pass  # 既定の選択を覚えるだけなので、書けなくても続ける
