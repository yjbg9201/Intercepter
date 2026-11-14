"""shogi-takeover: 将棋エンジンの対局中に操作権を人間が奪取・返却できる対局アプリ。

仕様は docs/SPEC.md、開発の決まりは CLAUDE.md。
依存の向き（SPEC §5.5）: __main__ → app → ui / transport / engine / record → core
"""

__version__ = "0.0.1"
