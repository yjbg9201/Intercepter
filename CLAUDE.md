# CLAUDE.md — shogi-takeover（リポジトリ名 Intercepter、パッケージ名 `takeover`）

将棋エンジンの対局（Floodgate などの CSA サーバ、またはローカルのエンジン同士）を観戦しつつ、任意のタイミングで自席の操作権をエンジンから奪って人間が指し、ボタン一つでエンジンに戻せる対局アプリ。

**仕様の正は `docs/SPEC.md`。** 作業前に必ず読むこと。実装中に決めたこと・判明した事実は SPEC の §12「未決事項」と「変更履歴」に追記する。

現在の状態：M0〜M5、M7、M9 を実装済み（ローカル対局・CSA 接続・Web UI・TimeGuard・ソークテスト・名前ストックの UI・連続参加・KIF 出力・24 点法）。v1 のうち LAN 公開のトークン認証、ヒント探索、予約切替、エンジン投了時の ask、スマホ向けレイアウトも入っている。未着手はローカル対局の「先後を入れ替えた連続対局」（SPEC §6.6）。未確認はスマホ実機での表示・操作と WSL2 のネットワーク設定（SPEC §11、§12）、M7 の機能の実サーバでの動作（偽 CSA サーバとブラウザでは確認済み。SPEC 変更履歴 v0.4.0）。実エンジン（やねうら王 MaterialLv1 の Linux ビルド、Windows .exe 2 種）と Floodgate（3 局。奪取・人間着手・返却・入玉宣言・TimeGuard・`hint.send_as_comment`）では M5 までの機能を確認済み（2026-11-16。SPEC 変更履歴 v0.3.4〜v0.3.8）。実サーバ（Floodgate / ローカル shogi-server）への接続は、ユーザーが明示的に頼んだときだけ行う。
各ファイルの役割は SPEC §5.5、各モジュール先頭の docstring（担当マイルストーンと参照すべき SPEC の節を書いてある）、テストの割り当ては `tests/README.md` を見る。

## ユーザーと環境

- ユーザーは将棋エンジンの運用・評価に詳しい。実装は基本的に任せる方針なので、報告は「何ができるようになったか」と「確認用のコマンド」を中心に、コードの説明は要点だけにする。
- 開発機は Windows 11 + WSL2（Ubuntu）。アプリ本体は WSL で動かす。
- エンジンは **Windows の .exe（WSL から直接起動）と Linux ビルドの両方**を使う。どちらでも同じように動くこと（SPEC §6.3、§11）。
- UI は WSL 内で起動し、Windows のブラウザで `http://localhost:8765` を開いて使う。v1 では同じ LAN のスマホからも使う（SPEC §6.7）。

## コマンド

変わったらここを更新する。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# python3-venv がない環境では: uv venv .venv && source .venv/bin/activate && uv pip install -e ".[dev]"
pytest -q                                          # 全テスト（ソークは少数局）
TAKEOVER_SOAK_GAMES=1000 pytest -m soak            # ソークテスト 1000 局（10 分前後）
python -m takeover local --config config.toml      # ローカル対局 + UI（http://localhost:8765）
python -m takeover local --config config.toml --headless   # UI なし。標準入力で take b / release b / move 7g7f / hint on / hint multi off など
python -m takeover csa   --config config.toml                   # UI の接続画面を開く（名前を選んで接続。名前の追加・編集もここ）
python -m takeover csa   --config config.toml --account test1   # その名前ですぐ接続 + UI
python -m takeover csa   --config config.toml --account test1 --continuous   # 連続参加（終局後に自動で次の対局を待つ）
python -m takeover fake-server --port 4081         # テスト用の偽 CSA サーバ
python -m takeover.fakes.fake_engine --help        # 偽 USI エンジン（子プロセスとして起動される側）
```

## 依存

`cshogi`, `fastapi`, `uvicorn[standard]`, `pytest`, `pytest-asyncio` のみ。追加するときは理由を SPEC の変更履歴に書く。
GUI ツールキット（Qt / Tk / wx など）は入れない。UI はブラウザ＋WebSocket。
HTTP / WebSocket のテストは `httpx`（FastAPI の `TestClient`）を使わず、uvicorn を空きポートで起動して `websockets`（`uvicorn[standard]` に含まれる）と `urllib` で接続する。

## 不変条件（破ったらバグ。テストで守る）

- **I1** 盤面・手番・時計・操作モードを持つのは GameCore だけ。他は読み取り専用のスナップショットを受け取る。
- **I2** エンジンプロセスに触るのは EngineController だけ。stdout の読み手は専用タスク1つ。
- **I3** すべての状態遷移は1つの asyncio イベントループ上で行う。スレッドは使わない（どうしても必要なら状態に触れない処理に限り `run_in_executor`）。
- **I4** エンジンには毎回 `position startpos moves ...`（または `position sfen ... moves ...`）の全手順を送る。エンジン内部の局面に依存しない。
- **I5** 探索ごとに search_id を振る。EngineController（STOPPING 中の bestmove）と GameCore（`expected_search_id` 不一致）の両方で古い bestmove を破棄する。
- **I6** 未完了の探索は常に1つまで。EngineController が IDLE でなければ `go` を送らない（呼ばれたら例外）。
- **I7** 1手番にコミットできる指し手は1つ。コミット（送信）した手は取り消さない。
- **I8** UI はコマンドを送ってスナップショットを描画するだけ。UI からの指し手は必ず GameCore で合法手チェックする。
- **I9** 子プロセスの stderr は `DEVNULL`。PIPE にして放置しない。
- **I10** USI・CSA の生の送受信行と内部イベントを、単調時刻付きで JSONL に記録する（ただし trip / password / UI トークンは伏せる）。
- **I11** ヒント探索（MANUAL 中の表示専用の探索）の結果は絶対にコミットしない。ヒントの search_id は `hint_search_id` に入れ、`expected_search_id` には入れない。

## 実装上の決まり

- イベント・コマンドは dataclass で型付けする。USI / CSA の行の解析・生成は純関数にしてユニットテストする。
- タイムアウト・猶予時間はすべて設定値にする（ハードコードしない）。
- 時間は内部では ms の int。経過計測は `time.monotonic()`。
- エンジン出力は各行の `\r` を除去してから解析する（エンジンの種類にかかわらず常に）。
- GameCore は `engine/base.py` の `Engine` と `transport/base.py` の `Transport`（どちらも Protocol）にだけ依存させる。`usi.py` / `csa.py` / `gateway.py` を `core/` から import しない（依存の向きは SPEC §5.5）。
- GameCore と Clock には時刻関数 `now` を注入できるようにする（テストでは偽の時計）。タイマーの発火もキューのイベントにする（SPEC §6.1）。
- エンジンへの指示は、状態更新のあとに「あるべき状態」との差分から導く関数（reconcile）1 か所に集める（SPEC §6.1）。
- CSA サーバへは、自分の手番でないときに指し手を絶対に送らない（サーバは捨てずに保留して次の手番に適用する。SPEC §6.5）。
- 千日手は cshogi の `is_draw()` を使わず 4 回を自前で数える。入玉宣言は cshogi の `is_nyugyoku()` を使う（SPEC §6.6）。
- エンジンのパス解決（Linux パス / `/mnt/c/...` / `C:\...`）は `engine/launch.py` の純関数にしてテストする。
- 子プロセスの終了は `quit` → 待機 → `kill` → `wait` まで必ず行う。Windows .exe は Windows 側に残っていないかも考慮する（SPEC §12）。
- 人間の手には、既定ではサーバへ送る指し手にコメントを一切付けない。人間の手の印は手元の棋譜・ログだけ（SPEC §6.5、§6.8）。例外は `hint.send_as_comment = true` のときだけで、ヒントの候補と一致した手にその評価値・読み筋を付ける（SPEC §6.9）。
- 評価値コメント（サーバへ送る `'*` と手元の棋譜）は先手視点（`csa.comment_score_pov = "black"` が既定。SPEC §6.5）。内部の `Score` は指した側視点のまま持ち、符号の変換は `csa_parse.comment_score` だけで行う。
- CSA モードの「対局の外側」（接続画面・名前ストックの編集・連続参加）は `session.py` の CsaSession に置く。1 局ごとに GameCore と CsaTransport を作り直し、GameCore にセッションの状態を持たせない（I1）。連続参加で自動的に再接続するのは、対局が成立して終局まで進んだときだけ（失敗・切断のあとは再接続しない）。
- 秘密情報は git 管理外のファイルに置く：`config.toml`（UI トークン等）と `accounts.toml`（名前ストック。trip / password を含む）。コミットするのは `config.example.toml` と `accounts.example.toml` だけ。`.gitignore` に `config.toml`、`accounts.toml`、`*.state.json`（最後に使った名前）、`logs/`、`records/` を入れる。UI から入力された trip / password も、UI・スナップショット・ログへ出さない。

## 過去実装で踏んだ不具合（M1 でテストとして先に書く）

| 症状 | 原因 | 対策 |
| --- | --- | --- |
| `stop` 直後に `bestmove resign` を返してしまう | stop 受信で待機を抜けていた | stop 後は `stop_grace_ms` まで bestmove を待つ |
| 前の局面の手を指す | キューに古い bestmove が残っていた | search_id で照合し、不一致は破棄（I5） |
| ponder が効かない | bestmove の ponder 手を捨てていた | `(move, ponder)` の組で扱う |
| 稀にフリーズ | stderr を PIPE にして読んでいない | `DEVNULL`（I9） |
| 起動失敗で永久に待つ | usiok / readyok 待ちに上限がない | タイムアウト＋プロセス死亡検知 |
| 指し手の取り違え | 思考処理が多重に走った | 未完了の探索は1つ（I6） |
| 対局間で状態が漏れる | 対局開始時にリセットしていない | `usinewgame` / `gameover` でリセット |
| ゾンビプロセス・ログ破損 | `quit` 後に wait していない | quit → wait → kill → wait |
| （想定）切替直後に誤着手 | 停止した探索の bestmove を採用した | GameCore 側でも `expected_search_id` で照合（I5） |

## 進め方

- SPEC §10 のマイルストーン順に1つずつ進める。各マイルストーンで：
  1. 完了条件に対応するテストを先に書く
  2. 実装する
  3. `pytest -q` が全部緑になるまで直す
  4. git commit（メッセージに M 番号）
  5. 「できるようになったこと／確認コマンド／未決事項」を短く報告する
- 通常のテストは `src/takeover/fakes/` の偽物で完結させる：偽エンジン（`fake_engine.py`、子プロセス）、スクリプト式エンジン（`scripted_engine.py`、プロセスなし）、偽 CSA サーバ（`fake_csa_server.py`）。
- 実エンジン・ローカル shogi-server・Floodgate への接続は、ユーザーが明示的に頼んだときだけ行う。
- 仕様にない判断が必要になったら、安全側（指し手を送らない・時間を浪費しない側）で仮決めし、SPEC §12 に記録して進める。ただし SPEC §7（切替の遷移表とレース規則）を変える場合は、先にユーザーに確認する。§7 に関する確認事項 Q1〜Q9（SPEC §12）は、2026-11-14 に仮決め案で進めることが承認済み（§12 の表が当面の仕様）。
- レース条件のテストは、GameCore についてはスクリプト式エンジンでキューへの投入順を直接決めて再現する。EngineController については偽エンジンの応答遅延の引数で順序を決める。`sleep` で偶然に頼るテストは書かない。

## やらないこと

- スレッドから GameCore やエンジンを触る
- エンジンに差分だけの局面指示を送る（毎回全手順）
- UI 側での合法手判定
- テストのために実 Floodgate へ接続する
- UI をトークンなしで `0.0.0.0` にバインドする、インターネットに公開する
- 宣言条件を満たさない状態で `%KACHI` を送る
- SPEC の対象外（検討モード、既存 GUI への組み込み、USI プロキシ方式、アプリ本体の Windows ネイティブ実行）に手を出す
