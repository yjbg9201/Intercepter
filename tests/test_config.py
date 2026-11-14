import tomllib
import warnings
from pathlib import Path

import pytest

from takeover.accounts import Account, AccountError, parse_accounts, select_account
from takeover.config import ConfigError, ConfigWarning, load_config, parse_config


def base() -> dict:
    return {
        "engine": {"main": {"path": "/x/engine", "options": {"Threads": 2}}, "sub": {"path": "/y/e"}},
        "local": {"black": "main", "white": "sub", "increment_sec": 30},
    }


def parse(data: dict):
    return parse_config(data, base_dir=Path("."))


def test_example_config_loads(repo_root):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        cfg = load_config(repo_root / "config.example.toml")
    assert cfg.engines["main"].options["USI_Hash"] == 1024
    assert cfg.engines["main"].ponder is True
    assert cfg.csa.extended_mode is False and cfg.csa.keepalive_sec == 60
    assert cfg.policy.max_restarts == 3
    assert cfg.base_dir == repo_root


def test_defaults_and_human_seat():
    data = base()
    data["local"]["white"] = "human"
    cfg = parse(data)
    assert cfg.local.white is None and cfg.local.black == "main"
    assert cfg.ui.host == "127.0.0.1" and cfg.time.margin_ms == 1000


@pytest.mark.parametrize(
    "patch",
    [
        {"uii": {}},
        {"ui": {"hots": "127.0.0.1"}},
        {"ui": {"port": "8765"}},
        {"ui": {"host": "0.0.0.0"}},  # LAN 無効のまま 0.0.0.0 は不可
        {"local": {"black": "nope", "white": "sub"}},
        {"csa": {"keepalive_sec": 10}},
        {"csa": {"comment_score_pov": "white"}},
        {"policy": {"on_engine_failure": "panic"}},
        {"time_guard": {"action": "explode"}},
        {"record": {"formats": ["pdf"]}},
        {"time": {"margin_ms": -1}},
        {"schedule": [{"plies": "8-1", "mode": "manual"}]},
        {"engine": {"main": {"path": "/x", "kind": "mac"}}},
        {"engine": {"main": {}}},
    ],
)
def test_invalid_config_rejected(patch):
    data = base()
    data.update(patch)
    with pytest.raises(ConfigError):
        parse(data)


def test_lan_allows_public_bind():
    data = base()
    data["ui"] = {"host": "0.0.0.0", "lan": {"enabled": True, "token": "t"}}
    assert parse(data).ui.lan.enabled


def test_time_guard_warning():
    data = base()
    data["local"]["increment_sec"] = 10
    with pytest.warns(ConfigWarning):
        parse(data)


def test_schedule():
    data = base()
    data["schedule"] = [{"plies": "1-8", "mode": "manual"}]
    entry = parse(data).schedule[0]
    assert (entry.first_ply, entry.last_ply) == (1, 8)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


# ---- 名前ストック ----


def test_example_accounts(repo_root):
    data = tomllib.loads((repo_root / "accounts.example.toml").read_text(encoding="utf-8"))
    accounts = parse_accounts(data)
    assert accounts[0].login_password() == "floodgate-300-10F,CHANGE_ME"
    assert accounts[1].login_password() == "anything"
    assert select_account(accounts, None, "test1").alias == "test1"
    assert select_account(accounts, "local", "test1").alias == "local"
    with pytest.raises(AccountError, match="test1"):
        select_account(accounts, "zzz", None)


def test_account_secrets_not_in_repr():
    account = Account(alias="a", name="n", game="g", trip="TRIPSECRET", password="PWSECRET")
    assert "TRIPSECRET" not in repr(account) and "PWSECRET" not in repr(account)
    assert set(account.secrets()) == {"TRIPSECRET", "PWSECRET"}
    assert "TRIPSECRET" not in str(account.public_view())


@pytest.mark.parametrize(
    "entry",
    [
        {"alias": "a", "name": "bad name", "password": "x"},
        {"alias": "a", "name": "x" * 33, "password": "x"},
        {"alias": "a", "name": "n"},
        {"alias": "a", "name": "n", "game": "g"},
        {"alias": "a", "name": "n", "password": "x", "extra": "y"},
        {"name": "n", "password": "x"},
    ],
)
def test_invalid_accounts(entry):
    with pytest.raises(AccountError):
        parse_accounts({"account": [entry]})


def test_duplicate_alias():
    entry = {"alias": "a", "name": "n", "password": "x"}
    with pytest.raises(AccountError):
        parse_accounts({"account": [entry, dict(entry)]})
