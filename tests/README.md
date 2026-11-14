# テストの割り当て

SPEC §9 のシナリオ番号と、テストファイルの対応。M 列が「済」のものは実装済み。
通常のテストは `src/takeover/fakes/` の偽物だけで完結させる（実エンジン・実サーバに接続しない）。

| ファイル | 対象 | シナリオ（SPEC §9） | M |
| --- | --- | --- | --- |
| `test_skeleton.py` | 構成の約束事（依存の向き、スレッド不使用、インターフェースの一致） | — | 済 |
| `test_config.py` | 設定・名前ストックの読込と検証、秘密が repr に出ない | — | 済 |
| `test_usi_parse.py` | USI 行の解析・生成 | 8（CRLF 除去後の解析） | 済 |
| `test_csa_parse.py` | CSA 行の解析・生成、LOGIN のマスク、コメントの符号 | 13 の純関数部分 | 済 |
| `test_eventlog.py` | JSONL、秘密のマスク | 22 のログ部分 | 済 |
| `test_fake_engine.py` | 偽エンジンが usi / isready / go / stop / quit に応答 | — | 済 |
| `test_launch.py` | パス変換（Linux / `/mnt/c` / `C:\`）、kind の判定 | 15 | 済 |
| `test_engine_controller.py` | 状態機械、search_id、cancel、再起動、quit | 1, 2, 5, 6, 7, 8, 21, 23、CLAUDE.md の過去不具合表 | 済 |
| `test_clock.py` | 消費時間、加算の順序、時間切れ、`go` の時間引数 | 4 の計算部分 | 済 |
| `test_nyugyoku.py` | 27 点法（24 点法の設定切替は M9） | 14 | 済 / M9 |
| `test_local_referee.py` | 千日手（4 回）、連続王手、最大手数、詰み | 20 | 済 |
| `test_game_core_transitions.py` | §7.1 の遷移表を 1 行ずつ（スクリプト式エンジン） | 1, 4, 5 | 済 |
| `test_game_core_races.py` | 同着、古い bestmove、二重コミット、古い ply | 2, 3, 9, 18 | 済 |
| `test_kifu.py` | CSA 棋譜、`mark_human_moves` | 12 の手元棋譜部分 | 済 |
| `test_local_game_e2e.py` | 偽エンジン同士がヘッドレスで終局まで。子プロセスが残らない | — | 済 |
| `test_csa_transport.py` | 偽サーバとの対局、コメント、手番外に送らない | 12, 13, 22 | 済 |
| `test_ui_messages.py` / `test_ui_gateway.py` | メッセージ変換、Origin / Host 検査、複数接続への配信 | 19（M6 で 17） | 済 |
| `test_time_guard.py` | TimeGuard の発動と再奪取の拒否 | 10 | 済 |
| `test_scripted_engine.py` / `test_fake_csa_server.py` / `test_rules.py` | 偽物そのものの動作、千日手の回数判定 | 20 | 済 |
| `test_soak.py`（`@pytest.mark.soak`） | ランダム注入の連続対局と不変条件 | ソーク | 済 |
| `test_hint.py` | ヒント探索の cancel、bestmove がコミットされない | 16 | 済 |

## 書き方の決まり

- 時間に関するテストは `now` を注入した偽の時計で行う。`sleep` で偶然に頼らない。
- GameCore のレースはスクリプト式エンジンで、キューへの投入順を直接決めて再現する。
- 子プロセスを使うテストは、終了後にプロセスが残っていないことまで確認する。
- ソークテストは乱数シードを局ごとに記録し、違反した局のイベントログを保存する。
