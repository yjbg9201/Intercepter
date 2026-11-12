# Intercepter（shogi-takeover）

将棋エンジンの対局（Floodgate などの CSA サーバ、またはローカルのエンジン同士）を観戦しつつ、
任意のタイミングで自席の操作権をエンジンから奪って人間が指し、ボタン一つでエンジンに戻せる対局アプリ。

- 仕様：[`docs/SPEC.md`](docs/SPEC.md)（仕様の正）
- 仕様の再精査メモ：[`docs/SPEC_REVIEW.md`](docs/SPEC_REVIEW.md)
- 開発の決まり：[`CLAUDE.md`](CLAUDE.md)
- テストの割り当て：[`tests/README.md`](tests/README.md)

現在は骨組み（ディレクトリ構成・型・インターフェース）のみ。実装はマイルストーン M0 から順に進める。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```
