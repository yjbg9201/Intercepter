"""テスト用の偽物（SPEC §9）。本体コードからは import しない（`fake-server` サブコマンドを除く）。

- fake_engine.py     : 子プロセスとして起動する偽 USI エンジン（EngineController のテスト用）
- scripted_engine.py : プロセスなしの engine.base.Engine 実装（GameCore のテスト用）
- fake_csa_server.py : 偽 CSA サーバ（CsaTransport のテスト用、`python -m takeover fake-server`）
"""
