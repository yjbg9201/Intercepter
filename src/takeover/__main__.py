"""CLI の入口: `python -m takeover <local|csa|fake-server>`。引数解析と app.py の呼び出しだけを行う。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__, app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="takeover", description="将棋エンジンの対局に人間が介入できる対局アプリ")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    local = sub.add_parser("local", help="ローカル対局（エンジン vs エンジン、両席とも奪取可能）")
    local.add_argument("--config", type=Path, default=Path("config.toml"))
    local.add_argument("--headless", action="store_true", help="UI を起動せず、標準入力のコマンドで操作する")

    csa = sub.add_parser("csa", help="CSA サーバ（Floodgate / shogi-server）に接続して対局")
    csa.add_argument("--config", type=Path, default=Path("config.toml"))
    csa.add_argument("--account", help="名前ストックの alias（省略時: UI の接続画面で選ぶ。ヘッドレスは csa.default_account）")
    csa.add_argument("--headless", action="store_true")
    csa.add_argument("--server", help="接続先（[[csa.servers]] の name かホスト名。省略時は [csa] host。UI の接続画面でも選べる）")
    csa.add_argument("--continuous", action=argparse.BooleanOptionalAction, default=None,
                     help="連続参加（終局後に自動で次の対局を待つ）。省略時は csa.continuous")

    fake = sub.add_parser("fake-server", help="テスト用の偽 CSA サーバ")
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--port", type=int, default=4081)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "local":
            return asyncio.run(app.run_local(args.config, headless=args.headless))
        if args.command == "csa":
            return asyncio.run(app.run_csa(args.config, account_alias=args.account, headless=args.headless,
                                           continuous=args.continuous, server=args.server))
        if args.command == "fake-server":
            return asyncio.run(app.run_fake_server(args.host, args.port))
    except NotImplementedError as exc:
        print(f"takeover {args.command}: 未実装です（担当マイルストーン: {exc}）", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
