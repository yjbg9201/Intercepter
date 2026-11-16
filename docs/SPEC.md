# shogi-takeover 仕様書（v0.3 ドラフト）

> このファイルが仕様の正。実装中に判明した事実・仮決めは §12「未決事項」と末尾の「変更履歴」に追記する。
> プロジェクト名は仮称（リポジトリ名は `Intercepter`、Python パッケージ名は `takeover`）。
> v0.3 での再精査の根拠と指摘一覧は `docs/SPEC_REVIEW.md`。

---

## 1. 目的

コンピュータ将棋の対局（Floodgate などの CSA サーバ上の対局、またはローカルのエンジン同士の対局）を観戦しつつ、**任意のタイミングで自分側のエンジンから操作権を奪って人間が指し、ボタン一つでエンジンに戻せる**対局アプリを作る。

背景：エンジン同士の対局は展開の幅が狭くなりがち。序盤の数手や終盤の局面に人間が介入し、エンジンが自分では選ばない局面を作って、その後の展開を観察したい。

---

## 2. 用語

| 用語 | 意味 |
| --- | --- |
| 席 (Seat) | 先手・後手それぞれの指し手の供給元 |
| 操作モード | `AUTO`（エンジンが指す）／`MANUAL`（人間が指す） |
| 奪取 / 返却 | AUTO→MANUAL / MANUAL→AUTO |
| コミット | 指し手を確定させた時点（CSA モードではサーバへ送信した瞬間）。以後取り消さない |
| Transport | 対局の進行役。CSA サーバ接続（CsaTransport）か、ローカル審判（LocalReferee） |
| 探索ID (search_id) | `go` ごとに振る連番。停止した探索の bestmove を識別して捨てるために使う |
| ヒント探索 | MANUAL の自手番に、表示専用でエンジンに考えさせる探索。結果は絶対にコミットしない |
| 名前ストック | CSA サーバへの参加に使う名前（アカウント）を複数登録しておく仕組み |

---

## 3. ユースケース

1. **観戦**：Floodgate にエンジンで参加し、終始 AUTO。盤・時計・読み筋を表示（既存 GUI と同等）。
2. **序盤介入**：最初の数手（例：1〜8手目）を人間が指し、以降エンジンに任せる。予約設定でもボタンでも可。
3. **途中介入**：任意の手番で奪取 → 人間が数手指す → 返却でエンジンに戻す。
4. **終盤介入**：エンジンが投了しそうな局面や入玉模様の局面で奪取して自分で指す。
5. **ローカル検証**：ネット接続なしで、ローカルのエンジン同士（先後どちらの席も奪取可能）で同じ操作を試す。開発・テストの主戦場。
6. **スマホから操作**：PC（WSL）でアプリを動かし、同じ LAN のスマホのブラウザから観戦・奪取・着手する。
7. **名前を使い分けて参加**：対局ごとに、ストックした名前から選んで Floodgate に参加する。

---

## 4. スコープ

**v0（最初に動かすもの）**
- ローカル対局モード（エンジン vs エンジン、両席とも奪取可能）
- CSA クライアントモード（Floodgate／ローカル shogi-server）
- エンジンは Windows .exe（WSL から起動）と Linux ビルドの両方に対応
- 名前ストック（設定ファイルに複数登録し、起動時に選択）
- 自席の AUTO/MANUAL 即時切替、ponder 対応
- ブラウザ UI（盤・持ち駒・時計・読み筋・棋譜・モード表示・切替ボタン・投了／宣言）
- 時間切れ保護（TimeGuard）
- 入玉宣言は 27 点法（ローカルモードの判定、人間の宣言ボタンの事前チェック）
- 全通信ログ、手元の棋譜保存（人間の手の記録はオプション）

**v1**
- LAN 公開（スマホから操作）＋トークン認証、スマホ向けレイアウト
- 名前ストックの UI からの選択・追加・編集
- 連続参加（終局後に再 LOGIN して次のマッチを待つ）
- MANUAL 中のヒント表示（オプション）
- 予約切替（手数範囲で MANUAL にするスケジュール）
- エンジン投了時に「投了するか自分で指し継ぐか」を選べるモード
- 入玉宣言の 24 点法
- KIF 形式の棋譜出力

**v2（構想）**
- 返却時の ponder 再開
- 任意局面からの開始（ローカルモード）

**対象外**
- 検討モード・棋譜解析（既存 GUI で足りる）
- 既存 GUI への組み込み、USI プロキシ方式（§5.3）
- Qt / Tk などのデスクトップ GUI ツールキット
- アプリ本体の Windows ネイティブ実行（本体は WSL / Linux で動かす）
- インターネットへの UI 公開

---

## 5. アーキテクチャ

### 5.1 全体図

```
┌──────── ブラウザ（PC / スマホ、複数同時接続可） ────────┐
│ 盤 / 時計 / 読み筋 / 棋譜 / ヒント / [奪取][返却][投了] │
└───────────────▲──────────────────────┬──────────────────┘
      状態スナップショット               │ コマンド
            (WebSocket)                 ▼
┌────────── Python プロセス（asyncio 単一イベントループ、スレッドなし）──────────┐
│                                                                              │
│  UiGateway ──コマンド──▶ ┌───────────────────────────┐ ◀──イベント── Transport │
│            ◀─スナップショット│ GameCore                  │ ──指し手送信─▶ (CSA or │
│                          │ 盤面・手番・時計・モードの  │               ローカル) │
│                          │ 唯一の持ち主（単一キュー）  │                        │
│                          └─────────┬─────────▲───────┘                        │
│                              手番通知│         │bestmove/info                   │
│                                    ▼         │                                 │
│                          Seat(先手) / Seat(後手)                                │
│                                    │                                           │
│                          EngineController ══ USI(stdin/stdout) ══ エンジン      │
│                                              （Linux バイナリ or Windows .exe） │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 設計の柱（前回試作の問題への対策）

| 症状 | 推定原因 | 本設計での対策 |
| --- | --- | --- |
| ponder 絡みの誤動作 | `stop` 後に返ってくる bestmove を「今の手」と取り違える／探索中に次の `go` を送る | 探索ID で古い bestmove を破棄（I5）。未完了の探索は常に1つ（I6）。EngineController の状態機械が `go` の送信可否を管理 |
| セグフォ | GUI ツールキットのオブジェクトを別スレッドから触る／同じパイプを複数箇所から読む／プロセスを複数コンポーネントで共有 | 単一 asyncio ループでスレッド不使用（I3）。エンジンプロセスの所有者は EngineController だけ（I2）。UI はブラウザ（別プロセス）に分離 |
| 切替後の盤面ずれ | エンジン内部の局面状態に依存している | 毎回 `position startpos moves ...` の全手順を送るステートレス方式（I4）。人間が何手指してもエンジン側との整合は不要 |

※原因は推定。前回のコードがあれば比較して確定させる。
※ I1〜I11 は CLAUDE.md の「不変条件」を指す。

**ステートレス方式の副産物**：エンジンが落ちても、再起動して `position`＋`go` をやり直せば対局を続けられる。

### 5.3 検討した代替案

- **USI プロキシ方式**（既存 GUI とエンジンの間に割り込み、人間の手を注入）：既存 GUI の盤を流用できるが、人間の入力経路が別窓になり、GUI 側の ponder・時計管理とプロキシの状態が二重化する。前回の不具合の温床なので不採用。
- **ShogiHome のフォーク**：CSA 通信対局・エンジン管理・盤 UI が揃っており有力。ただし Electron/TypeScript の大きなコードベースで、改造範囲（対局中の席の切替）が中核に及ぶ。単体アプリが行き詰まった場合の第二候補。
- **単体アプリ（採用）**：Python＋cshogi で全状態を自前で持つ。コードが小さく、Claude Code が全体を把握でき、偽エンジン・偽サーバで切替のレース条件を網羅的にテストできる。ブラウザ UI なのでスマホ対応も同じコードで済む。

### 5.4 技術スタック

- Python 3.11+（設定は標準の tomllib）、asyncio
- cshogi：合法手判定、SFEN/CSA/USI 変換、千日手判定など
- FastAPI＋uvicorn：HTTP と WebSocket
- フロントエンド：単一 HTML ファイル（素の JS、ビルド不要）。盤は SVG か CSS Grid、駒は漢字テキストで描画。PC とスマホで同じファイル（レスポンシブ）
- テスト：pytest＋pytest-asyncio。HTTP / WebSocket のテストは依存を増やさず、uvicorn を空きポートで実際に起動し、`uvicorn[standard]` に含まれる `websockets` と標準の `urllib` で接続する（FastAPI の `TestClient` は `httpx` が必要なので使わない）
- 実行環境：アプリ本体は WSL2 Ubuntu（または Linux）。エンジンは Linux バイナリと Windows .exe の両方（§6.3、§11）

### 5.5 ディレクトリ構成（v0.3 で確定）

```
Intercepter/
├── CLAUDE.md
├── docs/
│   ├── SPEC.md                # 仕様の正
│   └── SPEC_REVIEW.md         # v0.3 再精査の指摘一覧と根拠
├── pyproject.toml
├── config.example.toml        # config.toml は git 管理外
├── accounts.example.toml      # accounts.toml（名前ストック）は git 管理外
├── src/takeover/
│   ├── __main__.py            # CLI: local / csa / fake-server（引数解析と起動だけ）
│   ├── app.py                 # 組み立て：設定 → Engine / Transport / GameCore / UiGateway を配線して実行・終了処理
│   ├── session.py             # CSA モードの対局の外側：接続画面・名前ストックの編集・連続参加（1 局ごとに GameCore を作り直す）
│   ├── config.py              # 設定の dataclass と読込・検証
│   ├── accounts.py            # 名前ストックの読み書き
│   ├── core/
│   │   ├── types.py           # Color / Mode / Phase / Actor などの列挙と小さな値型
│   │   ├── events.py          # GameCore のキューに入るイベント・コマンド（dataclass）
│   │   ├── snapshot.py        # UI へ配る読み取り専用スナップショット
│   │   ├── game.py            # GameCore
│   │   ├── seat.py            # LocalSeat / RemoteSeat
│   │   ├── clock.py           # 時計と USI 時間引数の計算（純関数＋now 注入）
│   │   ├── rules.py           # 千日手の回数判定（cshogi の is_draw は使わない）
│   │   └── nyugyoku.py        # 入玉宣言判定（cshogi の薄いラッパ。27点法、v1 で 24点法）
│   ├── engine/
│   │   ├── base.py            # Engine インターフェース（Protocol）、EngineState、BestMove、InfoLine
│   │   ├── usi_parse.py       # USI 行の解析・生成（純関数）
│   │   ├── usi.py             # EngineController（base.Engine の実装。プロセスの唯一の所有者）
│   │   └── launch.py          # 起動コマンドの組み立て（Linux / Windows .exe、パス変換の純関数）
│   ├── transport/
│   │   ├── base.py            # Transport インターフェース（Protocol）
│   │   ├── local.py           # LocalReferee
│   │   ├── csa_parse.py       # CSA 行の解析・生成（Game_Summary、指し手行、コメント。純関数）
│   │   └── csa.py             # CsaTransport
│   ├── ui/
│   │   ├── gateway.py         # FastAPI, WebSocket, Origin 検査, トークン認証
│   │   ├── messages.py        # WebSocket メッセージ ⇔ コマンド／スナップショットの変換（純関数）
│   │   └── static/index.html
│   ├── record/
│   │   ├── eventlog.py        # JSONL（秘密情報のマスクを含む）
│   │   └── kifu.py            # CSA（v1 で KIF）書き出し
│   └── fakes/                 # テスト用の偽物（パッケージ内に置く。理由は下記）
│       ├── fake_engine.py     # 子プロセスとして起動する偽 USI エンジン（`python -m takeover.fakes.fake_engine`）
│       ├── scripted_engine.py # プロセスを使わない base.Engine の偽実装（GameCore のレース再現用）
│       └── fake_csa_server.py # 偽 CSA サーバ（`python -m takeover fake-server` の実体）
└── tests/
    ├── README.md              # §9 のシナリオ番号 → テストファイルの対応表
    ├── conftest.py
    └── test_*.py
```

**v0.2 案からの変更点と理由**
- 偽エンジン・偽 CSA サーバを `tests/fakes/` から `src/takeover/fakes/` へ移した。CLI の `fake-server` サブコマンドはインストール済みパッケージから import できる必要があり、`tests/` はパッケージに含まれないため。
- `engine/base.py` と `transport/base.py` にインターフェースを切り出した。GameCore は具象クラスではなくこのインターフェースにだけ依存する。これにより、同着レース（§9 シナリオ 3）を子プロセスの遅延ではなく `scripted_engine.py`＋キューへの投入順で決定的に再現できる。
- 行の解析・生成（`usi_parse.py` / `csa_parse.py` / `ui/messages.py`）を I/O を持つクラスから分離した（CLAUDE.md「純関数にしてユニットテストする」）。
- `app.py` を追加し、`__main__.py` から配線と終了処理（SIGINT / SIGTERM → 記録保存 → エンジン quit）を分離した。

**依存の向き**（逆向きの import を禁止する）

```
__main__ → app → ui / transport / engine / record → core
core は engine.base と transport.base（インターフェース）、および I/O を持たない純関数の engine.usi_parse だけを参照し、usi.py / csa.py / gateway.py を import しない
fakes は base のインターフェースと *_parse を使ってよいが、本体コードは fakes を import しない（fake-server サブコマンドを除く）
```

---

## 6. コンポーネント仕様

### 6.1 GameCore

- **唯一の状態保持者**：cshogi.Board、指し手履歴（各手に `actor` = engine / human / remote、消費時間、評価値、読み筋）、自分の手番色、各席のモード、時計、フェーズ、`expected_search_id`、`hint_search_id`。
- フェーズ：`CONNECTING → WAITING_GAME → READY → PLAYING → ENDED`
- 入力（UI コマンド、エンジンのイベント、Transport のイベント）はすべて1本の `asyncio.Queue` に入り、到着順に逐次処理する。複数の UI クライアント（PC とスマホ）からのコマンドも同じキューで直列化される。
- ハンドラ内の await は「エンジン停止完了待ち」など必要最小限。EngineController の読み取りタスクは GameCore のキューと独立しているので、ハンドラ内で `engine.cancel()` を待ってもデッドロックしない。
- 状態が変わるたびに、接続中のすべての UI へスナップショットを配信。
- bestmove イベントは、`search_id == expected_search_id` のときだけ採用する（二重チェック。§7.2）。`hint_search_id` の bestmove は表示にだけ使い、コミットしない（I11）。
- **タイマーもイベント**：TimeGuard の閾値到達は、GameCore が手番開始時に「期限タイマー」（`loop.call_later`）を1本張り、発火時に `TimerFired(kind, turn_token)` をキューへ入れて処理する。ローカルモードの時間切れは、サーバ役である LocalReferee が自分の時計とタイマーで検出し、CSA サーバと同じく `GameEnded` をキューへ入れる（v0.3.2 で変更。GameCore から見て両モードの区別をなくすため）。`turn_token` は手番ごとに変わる値で、古い手番のタイマーは不一致で無視する（タイマーのキャンセル漏れがあっても安全）。
- **時刻の注入**：GameCore と Clock は `now: Callable[[], float]`（既定 `time.monotonic`）を受け取る。時間に関するテスト（§9 シナリオ 4, 10）は偽の時計で決定的に行う。
- **CSA モードでの局面の進め方**：自分の手はサーバへ送信した時点（コミット点）で盤面に反映し、手番を相手に移す。その後に届くエコー（`+7776FU,T12`）は「送った手と一致するか」の検証と時計の確定に使う。不一致・`#ILLEGAL_MOVE` などはサーバの終局通知に従って終局処理する（盤面を巻き戻さない）。
- **1ゲーム1プロセス（v0）**：v0 ではプロセス起動 1 回につき 1 局。終局後も UI は閲覧用に残り、プロセス終了で終わる。連続対局は v1。
  - v1（M7、v0.4.0）：CSA モードは 1 回の起動で対局を繰り返せる。`session.py` の CsaSession が、1 局ごとに GameCore と CsaTransport を作り直す（盤面・手番・時計・操作モードを持つのは、その局の GameCore だけ。I1 は変わらない）。エンジンは起動したまま使い回し、対局ごとに `usinewgame` を送る。ローカルモードは従来どおり 1 起動 1 局。
- **設計指針（エンジンへの指示は「あるべき状態」から導く）**：イベントごとに個別の分岐でエンジンを操作するのではなく、状態更新のあとに「この席のエンジンは今なにをしているべきか（思考／ponder／ヒント／待機）」を1か所で計算し、現状との差分だけを `cancel()` / `think()` などに落とす関数（reconcile）を通す。§7.1 の遷移表はこの関数の入出力表として実装・テストする。エンジンが再起動中（§6.3）で指示できないときは何もせず、`EngineReady` イベントで再度 reconcile する。

### 6.2 Seat と操作モード

- `LocalSeat`：`mode`（AUTO/MANUAL）と `engine`（EngineController。人間専用席なら None）を持つ。
- `RemoteSeat`：CSA の相手。モードを持たず、奪取できない。
- ローカルモードでは両席が LocalSeat。CSA モードでは自席が LocalSeat、相手が RemoteSeat。
- 手番開始時：AUTO ならエンジンに思考依頼。MANUAL なら UI に合法手リストを送って入力待ち（ヒント有効ならヒント探索も開始。§6.9）。

### 6.3 EngineController（USI）

**状態機械**

```
STARTING ──usiok→オプション→readyok──▶ IDLE
IDLE ──think()──▶ THINKING ──bestmove──▶ IDLE
IDLE ──ponder()──▶ PONDERING ──ponderhit()──▶ THINKING
IDLE ──analyze()──▶ ANALYZING（ヒント探索。go infinite）
THINKING / PONDERING / ANALYZING ──cancel()──▶ STOPPING ──bestmove（破棄）──▶ IDLE
STOPPING ──stop_grace_ms 超過──▶ 強制終了 → 再起動 → IDLE
任意 ──プロセス終了──▶ DEAD（GameCore に通知。§7.4 の方針に従う）
```

**API（すべて async）**
- `start()`：起動 → `usi` → `usiok` → `setoption` 群 → `isready` → `readyok`。各待ちにタイムアウト。
- `new_game()`：`usinewgame`。`game_over(result)`：`gameover win|lose|draw`。
- `think(position_cmd, go_args) -> search_id`：IDLE でのみ受け付ける。
- `ponder(position_cmd, go_args) -> search_id`：`go ponder ...`。
- `ponderhit()`
- `analyze(position_cmd) -> search_id`：`go infinite`。ヒント探索用（v1）。
- `cancel()`：探索中なら `stop` を送り、その search_id の bestmove を受け取って破棄し、IDLE になるまで待つ。IDLE なら即 return。
- `set_options(dict)`：IDLE でのみ受け付ける（ヒント用の MultiPV 切替などに使う）。
- `quit()`：`quit` → wait（タイムアウトで kill → wait）。

**規則**
- stdout の読み手は専用タスク1つだけ。各行の `\r` を除去してから解析（エンジンの種類にかかわらず常に行う）。
- stderr は `DEVNULL`（PIPE にして読まないとフリーズする）。
- `info` は解析して GameCore へ。UI 向けは間引く。`lowerbound` / `upperbound` 付きの評価値は暫定値として区別。`multipv` 番号付きの info を区別して保持（ヒント表示用）。
- `bestmove X ponder Y` は `(X, Y)` で扱う。`resign` / `win` を区別。
- 未完了の探索があるのに `think()` / `ponder()` / `analyze()` が呼ばれたら、送信せず例外（バグとして検出）。
- USI オプションは設定ファイルから送る。`USI_Ponder` は ponder 設定に合わせる。
- `go` の時間引数：`btime wtime binc winc` または `btime wtime byoyomi`（§6.4）。
- **PONDERING 中に `stop` も `ponderhit` も送っていないのに bestmove が来た場合**（USI 違反だが、詰みを見つけたエンジンなどで起こりうる）：その bestmove は破棄して IDLE に戻り、ログに残す。その後に相手の手が予想手だった場合も `ponderhit` は送れないので、通常の `position`＋`go` を行う。
- **`cancel()` は再起動を待たない**：`stop` から `stop_grace_ms` 以内に bestmove が来なければプロセスを強制終了し、その時点で `cancel()` は return する（呼び出し側は最長でも `stop_grace_ms`＋kill の時間しか待たない）。再起動は EngineController 内のバックグラウンドタスクで行い、完了を `EngineReady`、失敗を `EngineDead` イベントで GameCore に知らせる。評価関数の読み込みに数十秒かかるエンジンでも、GameCore のキューは止まらない。
- 再起動後は `usinewgame` を送り直してから `position`＋`go` を受け付ける（I4 により局面の復元は不要）。
- 再起動は 1 局あたり `policy.max_restarts` 回まで。超えたら DEAD のままにして GameCore に通知する（再起動ループで持ち時間を使い切らないため）。
- stdout の `StreamReader` の上限（`limit`）は既定の 64KiB より大きくする（設定値。既定 1MiB）。長い `info ... pv` 行で `LimitOverrunError` になり読み取りタスクが止まるのを防ぐ。上限を超えた行は捨ててログに残し、読み取りは続ける。
- stdin への書き込みで `BrokenPipeError` / `ConnectionResetError` が出たら、プロセス終了と同じ扱い（DEAD）にする。例外を GameCore のハンドラまで漏らさない。
- `quit()` の手順：`quit` 送信 → 待機 → stdin を閉じる（EOF で終了するエンジンが多い。Windows .exe の取り残し対策を兼ねる）→ 待機 → kill → wait。

**起動（Linux / Windows 両対応）** — `engine/launch.py`
- `path` は次のどれでも受け付ける：Linux パス（`~/engines/foo`）、WSL から見た Windows パス（`/mnt/c/.../foo.exe`）、Windows 形式のパス（`C:\...\foo.exe`）。Windows 形式は `/mnt/<drive>/...` に変換する（`wslpath` があれば使い、なければ自前変換）。
- `cwd` を省略したら `path` のあるフォルダ。評価関数などの相対パス解決のため、Windows .exe では必須に近い。
- `.exe` は WSL の相互運用機能でそのまま起動できる。起動前に実行可能か（ファイルの存在・実行権限）を確認し、分かりやすいエラーを出す。
- 終了処理：Windows .exe の場合、WSL 側のプロセスを kill しても Windows 側にプロセスが残ることがある。`quit` → 待機 → kill の後、Windows 側に残っていないか確認する手段を用意する（§12）。
- 設定ファイルで `kind = "auto" | "linux" | "windows"`。`auto` は拡張子とパスで判定。

### 6.4 時計

- **ローカルモード**：`time.monotonic()` で自前計測。持ち時間・秒読み・フィッシャー加算に対応し、時間切れもローカルで判定。
- **CSA モード**：サーバの `T` 値（消費時間）を正とし、Game_Summary の `Time_Unit` / `Total_Time` / `Byoyomi` / `Increment` / `Least_Time_Per_Move` / `Time_Roundup` に従って各席の残りを計算。表示は手番開始時刻からのローカル経過を差し引いてカウントダウン。
- **USI に渡す時間**（`go` を送る時点で毎回計算する。奪取後の返却、ponder ミス後の再探索、再起動後のやり直しも同じ式）
  - `btime` / `wtime` = 残り時間 − 本手番の既経過 − `margin_ms`（下限0）。既経過を引くのは手番側だけ。
  - 既経過が残り時間を超えた分（秒読みに食い込んだ分）は `byoyomi` から引く。
  - `binc` / `winc` = Increment（フィッシャーの場合）
  - `byoyomi` = 秒読み − 食い込み分 − `margin_ms`（秒読みがある場合のみ。下限0）
  - `go ponder` の時間引数は「相手が即座に指したと仮定した次の自手番開始時」の値（＝現在の残り時間。既経過 0）。
  - 補足：やねうら王系の `NetworkDelay` / `NetworkDelay2` も同じ目的の余裕なので、`margin_ms` と二重に効く。両方を大きくしすぎない（§8 のコメント参照）。
- **shogi-server の時計（v0.3 で実装を確認。`shogi_server/time_clock.rb`、`game.rb`）**
  - 消費時間 `t = max(floor(経過秒), Least_Time_Per_Move)`。秒未満は切り捨て。
  - 着手ごとに `残り += Increment` してから `残り −= t`（下限0）。つまり**その手の加算分は、その手の思考に使える**。
  - 時間切れ判定は着手を受け取った時点で `残り − t + Byoyomi + Increment <= 0`（ちょうど 0 も負け）。
  - 確認したのは GitHub 上のミラー（TadaoYamaoka/shogi-server）。Floodgate 本番との差（`Time_Roundup` など）は実対局ログで検算するテストを用意する（§12）。
- `T` と Game_Summary の時間は `Time_Unit`（`1sec` / `1min` / `1msec` など）単位。内部では ms に換算する。

### 6.5 CsaTransport（CSA クライアント）

- 接続先：`host` / `port`（Floodgate は `wdoor.c.u-tokyo.ac.jp:4081`）。
- 参加に使う名前は名前ストック（§6.10）から選ぶ。
- **接続の順序**：エンジンを起動して `readyok` まで済ませてから LOGIN する（評価関数の読み込みが遅いエンジンで AGREE が遅れないようにする）。Game_Summary を受け取ったら `usinewgame` → `AGREE`（タイムアウト付き）→ `START:<GameID>` で対局開始。`REJECT:<GameID> by <name>` を受け取ったら WAITING_GAME に戻る。
- `LOGIN <name> <password>`。**既定では ` x1` を付けない**（通常の CSA モード）。
  - shogi-server は LOGIN 行に 4 つ目のトークンがあると x1 プロトコルに切り替わり、`<ゲーム名>,<識別文字列>` 形式のパスワードによる自動マッチ登録（`%%GAME` の自動発行）が行われなくなる（v0.3 で `login.rb` を確認）。x1 を使うなら自分で `%%GAME <ゲーム名> *` を送る必要がある。
  - 評価値コメント（`,'* ...`）は通常の CSA モードでも受理・記録される（`command.rb` の MoveCommand）。コメントのために x1 は不要。
  - 設定 `extended_mode`（既定 false）：true なら LOGIN に ` x1` を付ける（v0.5.0 で実装）。この場合、LOGIN だけでは対局待ちにならないので、通信欄（§6.11）から `%%GAME <ゲーム名> <+|-|*>` や `%%CHALLENGE …` を送って対局を申し込む。UI の接続パネルの「拡張モード（x1）」でも切り替えられる（次の接続から効く）。Floodgate の定時対局を待たずに、任意のゲーム名で相手とすぐ対局する用途を想定。
    - 拡張モードでも、LOGIN が通ったら名前ストックのゲーム名で `%%GAME <ゲーム名> *` を自動で送る（v0.5.1。通常モードでサーバが内部的に行う登録と同じ状態にする。別のゲーム名にしたいときは通信欄で `%%GAME` を送り直す）。ゲーム名のない名前（`password` だけ）では送らない。
    - 拡張モードの LOGIN のパスワード欄には、識別文字列（trip）だけを送る。通常モードではサーバが `<ゲーム名>,<識別文字列>` から識別文字列を取り出してパスワードにするが、拡張モードではパスワード欄がそのままパスワードになる（`login.rb`）。そろえないと、同じ名前でも別のプレイヤーとして扱われる。
  - `##` で始まる行（`##[WHO] …`、`##[ERROR] …` など）は拡張コマンドの応答。終局の理由行（`#RESIGN` など）として扱わず、ログと通信欄に出すだけにする。
- Floodgate のパスワード欄は `<ゲーム名>,<識別文字列>`（例：`floodgate-300-10F,xxxx`）。名前ストックの `game` と `trip` から組み立てる。
- LOGIN の応答：`LOGIN:<name> OK` / `LOGIN:incorrect`。失敗時は分かりやすいエラーで終了する（再試行しない）。
- LOGIN 名は英数字と `_` `-` のみ・32 文字以内（名前ストック読込時に検証する）。同じ名前で二重に LOGIN すると先の接続が切られるので、同名の同時起動はしない。
- `BEGIN Game_Summary` 〜 `END Game_Summary` を解析：`Protocol_Version`、`Format`、`Declaration`、`Game_ID`、`Name+` / `Name-`、`Your_Turn`、`Rematch_On_Draw`、`To_Move`、`Max_Moves`、`BEGIN Time` 〜 `END Time`（先後別の `BEGIN Time+` / `BEGIN Time-` もありうる。`Time_Unit`、`Total_Time`、`Byoyomi`、`Increment`、`Least_Time_Per_Move`、`Time_Roundup`、`Remaining_Time+` / `Remaining_Time-`）、`BEGIN Position` 〜 `END Position`（`PI`、`P1`〜`P9`、持ち駒行、途中手順を含む場合あり）。未知のキーは無視してログに残す。
- **指し手送信**：`+7776FU`。
  - エンジンの手：`,'* <評価値> <読み筋>` を付与できる（設定 `send_engine_comment`。`extended_mode` とは無関係）。
  - **人間の手：既定ではサーバへコメントを一切付けない。**人間の手であることは手元のログ・棋譜にだけ残す（§6.8）。
    - 例外（2026-11-16、ユーザーの要望で追加）：設定 `hint.send_as_comment = true` のとき、人間の手が現局面のヒントの候補（MultiPV のどれか）の初手と一致したら、その候補の評価値・読み筋をエンジンの手と同じ形式で付ける（§6.9）。「人間の手である」という印は、この場合もサーバへは送らない。
    - 例外その 2（2026-11-16、ユーザーの要望で追加）：通信欄（§6.11）で人間が入力した自由コメントは、次に指す自分の手 1 手にだけ付けて送る。評価値コメントより優先する。
  - **自分の手番でないときは絶対に送らない。**shogi-server は手番外に届いた指し手を捨てずに保留し、手番が来たら適用する（`game.rb` の Deferred 処理）。誤送信は「次の手として勝手に指される」事故になる（I7 の根拠）。
- **評価値コメントの符号**：**先手視点**（正なら先手が有利）。Floodgate の評価値グラフが先手視点で描かれるため（2026-11-16 の実対局で、指した側視点で送ると後手番のときにグラフが反転することをユーザーが確認。v0.4.0 で既定を変更）。エンジンの値（USI の `score cp` ＝探索時の手番側から見た値）を、後手の手では符号を反転して送る。`score mate N` は大きな値に写像する（±(100000 − N)）。符号の変換は `csa_parse.py` の 1 関数に閉じ込め、設定 `csa.comment_score_pov = "black" | "mover"`（既定 `black`。`mover` は指した側視点）で切り替えられる。手元の棋譜の評価値も同じ設定に従う（§6.8）。
- 相手の `'*` コメント：shogi-server は対局者には `<指し手>,T<秒>` だけを中継し、コメントは棋譜ファイルにだけ書く（`game.rb`）。**対局中に相手のコメントは届かない**前提で作る。届いた場合に備えて解析だけは行い、上と同じ規約で解釈して、UI で自分視点に表示するときは符号を反転する。
- 指し手受信：`+7776FU,T12` 形式。自分の手のエコーは**指し手部分**が送信内容と一致することを確認し（コメントはエコーに含まれない）、`T` で時計を更新。受信した空行（キープアライブの応答）は無視する。
- 終局：`#WIN` / `#LOSE` / `#DRAW` / `#CENSORED`、理由行 `#RESIGN` / `#TIME_UP` / `#ILLEGAL_MOVE` / `#SENNICHITE` / `#OUTE_SENNICHITE` / `#JISHOGI` / `#MAX_MOVES` / `#CHUDAN` など。
- 投了 `%TORYO`、入玉宣言 `%KACHI`。
- 終局の行は「理由行 → 結果行」の順（例：`#RESIGN` → `#WIN`）。結果行を受け取った時点で ENDED にする。`%TORYO` / `%KACHI` にも `,T<秒>` 付きのエコーが返る。
- キープアライブ：こちらからの送信が `keepalive_sec` 途絶えたら空行（LF のみ）を送る。CSA プロトコルは 30 秒未満の間隔での連続送信を禁じているので、設定値の下限は 30、既定は 60（shogi-server 自体は現状 30 秒規則を検査していないが、規約側に合わせる）。サーバは空行に空行で応答する。マッチ待ち（WAITING_GAME）の間も送る。
- **接続の繰り返しと連続参加**（v1、M7）：1 局が終わるたびに棋譜を保存し、`LOGOUT` して接続を閉じる。次の対局は新しい接続で LOGIN し直す。
  - 連続参加（設定 `csa.continuous`、CLI の `--continuous` / `--no-continuous`、UI のチェックボックス、ヘッドレスの `continuous on|off`）がオンなら、終局から `csa.rejoin_delay_sec` 秒待って、同じ名前で自動的に再 LOGIN する。
  - 自動で続けるのは、対局が成立して終局まで進んだときだけ。接続・LOGIN の失敗、対局中の切断のあとは再接続せず、接続画面に戻る（ヘッドレスは終了コード 1 で終わる）。サーバへ繰り返し接続し続けないため。
  - 対局待ち（LOGIN 済みで Game_Summary がまだ来ていない間）と、次の接続までの待ち時間は、UI の「待機をやめる」でやめられる（`LOGOUT` して接続画面へ）。対局中は切断できない（やめるなら投了）。
  - LOGIN が通ったら Transport は `LoggedIn` を出し、GameCore のフェーズは CONNECTING → WAITING_GAME になる。
- 対局中に切断されたら記録を保存して終了（自動再接続は対象外）。
- アプリの終了（SIGINT / SIGTERM）：対局中なら切断＝負けになるので、コンソールに警告を出したうえで、記録保存 → `LOGOUT` → エンジン `quit` の順に片付ける。
- ログに残す LOGIN 行はパスワード部分を伏せる（I10）。

### 6.6 LocalReferee（ローカル対局）

- 両席とも LocalSeat。初期局面は平手（任意局面は v2）。
- 合法性・千日手（連続王手の千日手を含む）・入玉宣言・最大手数・時間切れを判定。cshogi で賄えるものは cshogi を使う。
- **入玉宣言は 27 点法が既定**。24 点法は設定 `local.nyugyoku_rule = "27" | "24"` で切り替える（v1、M9 で実装）。
games = 1                      # 続けて指す局数（2 以上で連続対局。1 局ごとに棋譜を保存し、通算成績を出す）
alternate_sides = true         # 連続対局で 1 局ごとに先後を入れ替える
next_game_delay_sec = 3        # 終局から次の局を始めるまでの間隔
  - 24 点法：点数以外の条件は 27 点法と同じ。31 点以上で宣言した側の勝ち、24〜30 点は持将棋（引き分け）、23 点以下の宣言は負け。先後で点数は変わらない。引き分けになる宣言も「有効な宣言」として、宣言ボタンとエンジンの `bestmove win` を受け付ける。UI は確認ダイアログで「引き分けになる」ことを知らせる（スナップショットの `declare_result` = `"win"` / `"draw"`）。
  - CSA モードは常に 27 点法（サーバの規則）。
  - 27 点法の条件：宣言側の手番であること、玉が敵陣（3段目以内）にいること、王手されていないこと、敵陣内に玉以外の自駒が10枚以上あること、点数（大駒5点・小駒1点、敵陣内の駒と持ち駒が対象）が先手28点以上・後手27点以上であること、持ち時間が残っていること。
  - cshogi 1.1.1 には `Board.is_nyugyoku(rule)` / `Board.nyugyoku_result(rule)`（`cshogi.LAW_27` / `cshogi.LAW_24`）がある（v0.3 で確認）。`core/nyugyoku.py` はこれの薄いラッパとし、cshogi が見ない条件（持ち時間が残っていること）だけ足す。§9 シナリオ 14 の各ケースで cshogi の判定が上の条件と一致することをテストで確かめる。
- **千日手は自前で数える**：cshogi の `Board.is_draw()` は探索向けの判定で、同一局面の **2 回目**の出現で `REPETITION_DRAW` を返す（v0.3 で確認。玉の往復 4 手で成立する）。対局規則の千日手（同一局面 4 回）には使えない。LocalReferee は局面キー（`zobrist_hash()`。盤・持ち駒・手番を含むこと）の出現回数を自分で数え、4 回目で千日手とする。連続王手の千日手は、同一局面の 1 回目から 4 回目までの間、一方の指し手がすべて王手だったかを履歴から判定する（王手をかけ続けた側の負け）。
- エンジンのルール設定も揃える（例：やねうら王系なら `EnteringKingRule` を 27 点法相当にする。値はエンジンのドキュメントで確認）。
- **先後を入れ替えた連続対局**（v1。v0.4.2 で実装）：設定 `local.games`（局数、既定 1）、`local.alternate_sides`（1 局ごとに先後を入れ替える、既定 true）、`local.next_game_delay_sec`（次の局までの間隔、既定 3）。
  - 1 局ごとに GameCore・LocalReferee・エンジンを作り直す（エンジンは局ごとに `quit` して起動し直す。色が入れ替わるうえ、前の局の設定や状態を持ち越さないため）。操作モードも毎局、エンジンのある席は AUTO から始まる。
  - 棋譜は 1 局ごとに保存する。イベントログは連続対局で 1 本。各局の開始時に「第 n 局（全 N 局）: 先手 X / 後手 Y」、終局のたびに通算成績（設定上の先手席・後手席ごとの勝ち数と引き分け数）を通知する。
  - 途中の局でエンジンが起動できなければ、そこで終わる（終了コード 1）。最後の局のあとは、これまでどおり UI を閲覧用に残す。

### 6.7 UI

**画面構成**（PC 横長とスマホ縦の両方を同じ HTML で。スマホ対応は v1 で仕上げる）
- 盤（自分の色が下、反転ボタン）と両者の持ち駒
- 時計（両席、手番側を強調、残りわずかで警告色）
- **モード表示**：各 LocalSeat に大きなバッジ `AUTO`（緑）／`MANUAL`（橙）
- **切替ボタン**：席ごとのトグル。キーボード `Space` でも切替（CSA モードは自席、ローカルモードは手番側）
- 投了・宣言勝ちボタン（MANUAL の自手番のみ有効、確認ダイアログ付き）。宣言勝ちボタンは GameCore が宣言条件を満たすと判定したときだけ押せる
- エンジン情報：深さ・評価値（自分視点）・ノード数・NPS・読み筋（日本語表記）
- ヒント欄（v1、オプション）：§6.9
- 棋譜リスト：各手の actor（人間／エンジン）を記号で表示、消費時間、評価値
- 通知欄：「送信済みのため奪取は次の手番から有効」「TimeGuard で AUTO に戻しました」など
- 接続画面（CSA モード、v1。M7 で実装）：画面上部の接続パネル。未接続のときは名前ストックの一覧から選んで「接続」、「名前の編集…」で追加・編集・削除のダイアログ。接続中は名前と状態（接続中／対局待ち／対局中／次の接続待ち）を出し、対局待ちの間だけ「待機をやめる」を出す。連続参加のチェックボックスは常に出す。対局がまだないときは平手の初期配置を薄く表示し、終局後は次の対局が決まるまで前の対局の終局図を見せておく。

**盤操作**
- 駒をクリック（タップ）→ 移動先候補をハイライト → クリックで着手
- 成／不成は選択ダイアログ（成りが強制のときは自動）
- 持ち駒をクリック → 打てるマスをハイライト
- 合法手リストはサーバから受け取る。UI 側では合法性を判定しない

**スマホ向け（v1）**
- 縦画面で盤を最大化し、切替ボタン・投了ボタンは親指で届く位置に大きく配置（M7 と同時に実装：幅 760px 以下では画面下に固定のバーを出し、奪取／返却・投了・宣言を並べる。盤の幅はスクロールバーに左右されないようコンテナ幅から決める。ローカル対局は両席を操作するので、切替は席ごとのボタンのまま）
- 誤操作防止：奪取・返却は1タップ、投了・宣言は確認付き
- MANUAL の自手番になったら通知（音、ブラウザが許す範囲で振動）。設定でオフにできる（設定 `ui.notify_my_turn` が既定値。端末ごとに UI の「手番の通知」ボタンでオン／オフでき、ブラウザに記憶する）
- PC とスマホの同時接続可。どちらからの操作も同じキューで直列処理され、両方に同じ状態が表示される

**WebSocket メッセージ（JSON）**
- サーバ→UI：`snapshot`（`rev`（スナップショットの通し番号）、`ply`（現在の手数）、SFEN、指し手履歴、対局者名、自分の色（CSA モード）、各席の種別とモード、時計、フェーズ、合法手（人間が指せるときのみ）、宣言可否、最終手、ヒントのオン／オフ、終局結果）、`engine_info`（最大5回/秒に間引き）、`hint`、`notice`、`log`、`session`（CSA モードのみ：`state`（`lobby` / `connecting` / `waiting` / `playing` / `rejoin_wait`）、サーバ名、接続中の名前、名前ストックの一覧（alias・LOGIN 名・ゲーム名・メモだけ）、既定で選ぶ名前、連続参加のオン／オフ、接続画面に出す文言）
- UI→サーバ：`move {color, ply, usi}`、`set_mode {color, mode}`、`resign {color, ply}`、`declare {color, ply}`、`set_hint {enabled}`、`set_hint_multipv {enabled}`
  - セッション操作（CSA モード。GameCore ではなく CsaSession が受ける）：`connect {alias}`、`disconnect`、`set_continuous {enabled}`、`save_account {alias, name, game, trip, password, memo, original_alias}`、`delete_account {alias}`。`save_account` の `original_alias` があれば編集、なければ追加。編集で `trip` / `password` が空なら元の値を保つ（UI には秘密を出さないので、空は「変更なし」の意味）。
  - 通信欄（CSA モード。§6.11）：UI→サーバ `raw_csa {line}`、`set_extended {enabled}`。`move` には `comment`（次の手に付ける自由コメント）を付けられる。サーバ→UI `csa_log {reset, lines: [{dir, line, time}]}`（`reset` が true なら表示を置き換える。新しい接続には、それまでの分を 1 通で送る）。
  - `color` は対象の席。ローカルモードは両席が LocalSeat なので必須。RemoteSeat に対するコマンドは拒否する。
  - `ply` は UI がそのコマンドを作ったときに表示していたスナップショットの `ply`。GameCore は現在の `ply` と一致しないコマンドを拒否して `notice` を返す。古い画面から遅れて届いた着手が、たまたま次の自手番でも合法だった場合に指されてしまう事故を防ぐ（§7.2 の「2台目の端末から遅れて届いた着手も拒否」を、相手の手が挟まった後にも成り立たせる）。
  - `set_mode` はトグルではなく目標モードを指定する。2 台から同時に押しても二重反転しない。
  - `notice` はコマンドを送った接続だけでなく全接続に配る（誰の操作が拒否されたかを全員が見られる）。
- UI は受け取ったスナップショットを描画するだけのステートレスな描画器。再読み込み・再接続時は最新スナップショットで完全に復元できること。
- 秘密情報（trip / password / UI トークン）はどのメッセージにも含めない。

**ローカル接続の保護（v0 から）**
- `127.0.0.1` にバインドしていても、ブラウザで開いた別サイトのスクリプトは `ws://localhost:8765` へ接続を試みられる（WebSocket には同一オリジン制約がない）。投了や着手を勝手に送られないよう、WebSocket のハンドシェイクで `Origin` ヘッダを検査し、自分自身（`http://localhost:<port>` / `http://127.0.0.1:<port>`、LAN 公開時は設定した許可ホスト）以外は拒否する。
- HTTP も `Host` ヘッダが想定外なら拒否する（DNS リバインディング対策）。
- 状態を変える操作は WebSocket のコマンドだけにし、GET / POST の HTTP エンドポイントでは状態を変えない。

**LAN 公開と認証（v1）**
- 既定は `127.0.0.1` にバインド。`ui.lan.enabled = true` で `0.0.0.0` にバインドし、トークン認証を必須にする。
- トークンは設定値、空なら起動時にランダム生成。起動時にアクセス用 URL（トークン付き）をコンソールと PC 側 UI に表示する。QR コード表示は依存を増やさずにできる場合のみ（§12）。
- トークンのない HTTP / WebSocket 接続は拒否する。
- 家庭内 LAN での利用が前提。インターネットへの公開はしない。

### 6.8 記録・ログ

- **イベントログ**：USI・CSA の全送受信行と内部イベント（モード変更、search_id の払い出し、破棄した bestmove など）を、単調時刻付きの JSONL で `logs/` に保存。レース不具合の調査に使う。
  - 秘密情報は書き込み前に伏せる（I10）：CSA の `LOGIN` 行のパスワード部分、UI トークン（URL のクエリを含む）。マスクは `record/eventlog.py` の純関数にしてテストする。
  - 1 行ごとに flush する（異常終了時に直前の行が残るように）。
- **棋譜**：対局ごとに手元に保存する。形式は `record.formats` で選ぶ（複数可）：`csa`（.csa）、`kif`（.kif、Shift_JIS）、`kifu`（.kifu、UTF-8。1 行目に `#KIF version=2.0 encoding=UTF-8`）。KIF は M7 で実装。
  - 評価値の視点は `csa.comment_score_pov`（既定は先手視点。§6.5）。CSA は `'** 評価値 読み筋`、KIF は `*評価値 +123 ▲７六歩 …`。
  - KIF の終局は、投了・切れ負け・詰み・千日手・持将棋・入玉勝ち・反則勝ち／反則負け（手番側から見て）・中断で書く。平手以外の開始局面は局面図で書く。
  - 同じ名前のファイルがあるときは上書きせず、末尾に `_2`、`_3` … を付ける（連続参加で同じ秒に終わる対局があっても失わない）。
  - エンジンの手には評価値・読み筋をコメントで残す。
  - 人間の手の記録：`record.mark_human_moves = true` で、人間の手のコメントに印（例：`'human`）を付ける。オフなら印を付けない。イベントログには設定にかかわらず常に actor を残す。
  - **サーバ側の棋譜には人間の印を送らない**（§6.5）。
- ファイル名：`<日時>_<GameID または local>_<先手名>_<後手名>.csa`（名前・GameID はファイル名に使えない文字を `_` に置換する）
- 棋譜は終局時だけでなく、切断・SIGINT・例外での終了時にもそこまでの手順を保存する。

### 6.9 ヒント表示（v1、オプション）

- 設定 `hint.enabled`（既定 false）。UI から対局中にオン／オフできる。
- MANUAL の自手番が始まったら、自席のエンジンで `go infinite`（`analyze()`）し、候補手・評価値・読み筋を表示する。
- MultiPV：`hint.multipv`（既定 1）。1 以外のときは、ヒント探索の前に `set_options` で MultiPV を変更し、AUTO の `go` の前に必ず元の値へ戻す（IDLE 時のみ変更可能）。
  - 注意：USI の規約上 `setoption` は `isready` の前に送るもので、対局中の変更を受け付けるかはエンジン依存（やねうら王系の MultiPV は待機中なら通る）。受け付けないエンジンでは MultiPV を 1 に固定する設定にできること。
- 人間の着手・返却・終局・ヒントのオフで `cancel()` する。ヒント探索の bestmove は破棄し、コミットしない（I11）。
- 相手手番中はヒント探索をしない。
- **候補数の切替**（2026-11-16 追加）：`hint.multipv` が 2 以上のとき、対局中に MultiPV を設定値と 1 の間で切り替えられる（UI の「候補数」ボタン＝`set_hint_multipv {enabled}`、ヘッドレスは `hint multi on|off`）。ヒント探索中に切り替えたら、探索を `cancel()` → `set_options` → 同じ局面で `analyze()` をやり直す。スナップショットに `hint_multipv`（いまの値）と `hint_multipv_max`（設定値）を入れる。
- **ヒントの読み筋をコメントにする**（2026-11-16 追加）：`hint.send_as_comment`（既定 false）。true のとき、人間が指した手が現局面のヒントの候補の初手と一致すれば、その候補の評価値・読み筋を指し手に付ける（CSA サーバへの送信は `csa.send_engine_comment` も true のとき。手元の棋譜にも `'**` で残る）。
  - 候補にない手・ヒントがオフ・まだ info が届いていないときは何も付けない（評価値を作らない）。暫定値（lowerbound / upperbound）は使わない。前の局面のヒントは持ち越さない。
  - ヒント探索の bestmove をコミットしないこと（I11）は変わらない。付けるのは人間が選んだ手に対する表示用の情報だけ。

### 6.10 名前ストック

- 名前は固定しない。複数の名前を `accounts.toml`（git 管理外）に登録しておき、接続ごとに選ぶ。
- 1エントリの項目：`alias`（選択用の短い名前）、`name`（LOGIN 名）、`game`（例 `floodgate-300-10F`）、`trip`（識別文字列）、`password`（Floodgate 以外のサーバ用。指定時は `game,trip` より優先）、`memo`。
- v0：CLI で `--account <alias>` を指定。省略時は `default_account`。
- v1（M7 で実装）：UI の接続画面で一覧から選択、追加・編集・削除。最後に使ったものを既定にする。
  - `--account` を付けて起動すると、その名前ですぐ接続する（従来どおり）。付けずに UI ありで起動すると、接続画面を開いて待つ。ヘッドレスで付けなければ `csa.default_account`。
  - 名前ストックのファイルがまだなくても、UI ありなら起動できる（接続画面で最初の名前を登録する）。
  - UI から編集すると `accounts.toml` を書き直す（一時ファイルに書いてから置き換え、権限は 600）。手書きのコメントは残らない。検証は読込時と同じ規則。
  - 最後に使った alias は `accounts.toml` の隣の `accounts.state.json` に置く（秘密は入れない。git 管理外）。
  - UI から入力された trip / password は、保存した時点でイベントログの伏せ字の対象に加える。
- 秘密情報（trip / password）は UI・ログ・スナップショットに出さない。


### 6.11 サーバとの通信欄（CSA モード。v0.5.0 で追加）

将棋所のデバッグウィンドウや ShogiHome の CSA ウィンドウに当たるもの。画面の下部に置く。

**通信ログ**
- CsaTransport が送受信した行を、時刻・方向（送信／受信）つきで表示する。CsaSession が直近 500 行を覚えておき、後から繋いだ画面にも渡す（対局をまたいで残る）。
- LOGIN 行はパスワード部分を伏せたもの（`LOGIN <name> ***`）だけを UI へ渡す。trip / password は通信欄にも出さない。

**サーバへ送る 1 行**（入力欄。よく使うコマンドはプルダウンから入力欄に入る）

サーバ（shogi-server）は `+` `-` `%X` で始まる行を「指し手」として扱い、手番外に届いたものを捨てずに保留して、次の手番に適用する（`game.rb` の Deferred 処理）。そのため、入力された行は種類ごとに次のように振り分ける（`csa_parse.classify_raw_line` と 1 対 1）。

| 入力 | 扱い |
| --- | --- |
| `+7776FU` など（`+` `-` で始まる） | 送らない。指し手は盤から指す（合法手チェック・手番の検査を必ず通すため） |
| `%TORYO` | 投了ボタンと同じ経路（ResignCommand）。MANUAL の自手番にだけ送る |
| `%KACHI` | 宣言ボタンと同じ経路（DeclareCommand）。宣言条件を GameCore が確かめ、満たさなければ送らない |
| その他の `%X`（`%HIKIWAKE` など） | RawSpecialCommand。投了と同じ検査（対局中・自席・最新の画面・自手番・MANUAL）を通して、1 手番 1 回のコミットとして送る（I7）。UI は送る前に確認する。**shogi-server は `%TORYO` / `%KACHI` 以外を知らず、非合法手（反則負け）として扱う**（v0.5.0 の時点でミラーのソースを確認）。対応しているサーバ向けの機能 |
| `'` で始まる行（コメント） | サーバへ単独では送らない（送っても `##[ERROR] unknown command` が返るだけで、棋譜に残らない）。UI が覚えておき、次に指す自分の手 1 手に付けて送る。続けて入力すると複数行になる（8 行まで）。詳細は下の「コメントの形」 |
| `%%` で始まる行（`%%WHO` `%%LIST` `%%GAME` `%%CHALLENGE` `%%CHAT` など） | そのまま送る。対局の進行に影響しないので、手番やモードに関係なく、接続中ならいつでも送れる |
| `LOGIN …` | 送らない（秘密を含む。接続は接続パネルから） |
| `LOGOUT` | 「待機をやめる」と同じ（対局待ちの間だけ。対局中は切断しない） |
| `AGREE` / `REJECT` | 送らない（アプリが自動で送る） |
| それ以外 | そのまま送る（サーバは `##[ERROR]` を返すだけ） |

- 入力は 1 行・200 文字まで。制御文字（改行を含む）は取り除く（1 回の入力で 2 行送らせない）。

**コメントの形**（v0.5.1。Floodgate での確認を反映）
- 入力するのは「サーバの棋譜に残したい形」。評価値コメントは `'** <評価値> <読み筋>`、評価値や読み筋と関係のないフリーコメントは `'* <文>`。`'` だけで始まる行は `'*` に直し、`'` で始まらない文には `'* ` を補う。
- shogi-server は、指し手の次の区切りが `'X` なら棋譜に `'*X` と書く（`*` を 1 つ足す。`command.rb` の MoveCommand）。そのため 1 つ目のコメントは `*` を 1 つ減らして送る：残したい `'** 31511 -5251OU` → 送る `,'* 31511 -5251OU`、残したい `'* メモ` → 送る `,' メモ`。（v0.5.0 は入力をそのまま送っていたので、Floodgate の棋譜で `'*** …` になった。）
- `,` は `、` に置き換える（サーバは指し手行を `,` で区切るため）。
- 複数行は `,` で続けて 1 つの指し手行で送る（`+7776FU,' 一行目,'* 二行目`）。2 行目以降は残したい形のまま送る。**公開されている shogi-server のソース（確認した 4 つの複製すべて）は、1 つ目の区切りしか棋譜に書かない。** Floodgate の稼働版が 2 行目以降を残すかは未確認。手元の棋譜には全行を残す。
- プルダウンから外してあるコマンド（HTML のコメントにしてあり、戻せる。入力欄に直接打てば送れる）：`%HIKIWAKE`（サーバが未対応で反則負けになる）、`%%SHOW`（結果を通信欄の文字でしか見られない）、`%%RATING`（Floodgate で送ると接続が切れる。2026-11-17 にユーザーが再現を確認）。
- ヘッドレスでは標準入力の `raw <行>` が同じ振り分けを通る。
- エンジンが指している間（AUTO）は、`%X` を送れない（先に奪取する）。`%%` の拡張コマンドは AUTO のままでも送れる。

---

## 7. 操作権切替の仕様（中核）

### 7.1 遷移表（LocalSeat について）

| 状況 | イベント | 処理 |
| --- | --- | --- |
| 自手番・AUTO・思考中 | 奪取 | `engine.cancel()`（bestmove は破棄）→ MANUAL → 合法手を UI へ（ヒント有効ならヒント探索開始） |
| 自手番・AUTO・思考中 | bestmove 着 | 合法手チェック → コミット → 相手手番へ。ponder 有効なら `X Y` の局面で ponder 開始 |
| 自手番・MANUAL | 人間が着手 | （ヒント探索中なら `cancel()`）→ GameCore で合法手チェック → コミット |
| 自手番・MANUAL | 返却 | （ヒント探索中なら `cancel()`）→ 本手番の既経過を差し引いた時間で `position`＋`go` → AUTO |
| 自手番・MANUAL | ヒントのオン／オフ | オンなら `analyze()`、オフなら `cancel()` |
| 相手手番・AUTO・ponder 中 | 奪取 | `engine.cancel()` → MANUAL |
| 相手手番・AUTO・ponder 中 | 相手の手＝予想手 | `ponderhit` |
| 相手手番・AUTO・ponder 中 | 相手の手≠予想手 | `cancel()` → `position`＋`go` |
| 相手手番・AUTO・待機 | 相手の手 | `position`＋`go` |
| 相手手番・MANUAL | 返却 | AUTO にするだけ（ponder はしない。v2 で検討）。相手の手が来たら `go` |
| 相手手番・MANUAL | 相手の手 | 自手番・MANUAL へ（ヒント有効ならヒント探索開始） |
| 自手番・MANUAL | 残りが TimeGuard 閾値未満 | （ヒント探索中なら `cancel()`）→ 自動で返却（AUTO）して通知 |
| いずれか | 終局 | `cancel()` → `gameover` → 記録保存 |
| いずれか | エンジン異常 | §7.4 |

### 7.2 レース条件の規則

- すべてのイベントは単一キューで直列処理される。「同時」は存在せず、先にキューに入った方が先に処理される。PC とスマホからの同時操作も同じ。
- **コミット点**＝指し手を確定した瞬間（CSA ではサーバへ送信した瞬間）。奪取がコミット後に処理された場合、その手は有効のまま、モードだけ MANUAL になって次の自手番から人間が指す。UI に「送信済みのため次の手番から」と通知する。
- 奪取の処理では `expected_search_id` をクリアする。キューに既に入っていた bestmove イベントは、後で処理されても search_id 不一致で破棄される（ログに残す）。
- EngineController 側でも、STOPPING 中に来た bestmove は破棄する（二重の防御）。
- ヒント探索の search_id は `hint_search_id` に入れ、`expected_search_id` には入れない。ヒントの bestmove がコミット経路に入ることはない（I11）。
- 返却直後に再奪取しても cancel で処理されるだけで、不整合は起きない。
- 1手番にコミットできるのは1手だけ。コミット後は相手の手が来るまで、その席からの着手を拒否する（2台目の端末から遅れて届いた着手も拒否される）。

### 7.3 予約切替（v1）

```toml
[[schedule]]
plies = "1-8"     # 通し手数で指定。自分の手番の手だけが対象
mode = "manual"
```

- 自手番開始時に評価し、該当すれば MANUAL にする。スケジュールが MANUAL にした場合に限り、範囲を抜けたら AUTO に戻す。
- その手番中に手動で切り替えた場合は、手動操作を優先する。

### 7.4 TimeGuard・エンジン異常・宣言

- **TimeGuard**：`time_guard.min_remaining_ms`。MANUAL の自手番で（残り時間＋今回使える加算・秒読み）がこれを下回ったら、`action = "auto"` なら返却、`"warn"` なら警告のみ。0 で無効。
- **エンジン異常**（異常終了・stop 無応答・非合法な bestmove）：`on_engine_failure = "restart" | "manual"`。
  - `restart`：プロセスを再起動し、`readyok` 後、自手番なら `position`＋`go` をやり直す。再起動に失敗した、または時間が足りない場合は MANUAL にして警告。
  - `manual`：即 MANUAL にして警告。
- **エンジンの投了**（`bestmove resign`）：`on_engine_resign = "resign" | "ask"`。`ask` は MANUAL に切り替えて通知し、人間が投了するか指し継ぐかを選ぶ（v1）。
- **エンジンの宣言**（`bestmove win`）：`%KACHI` を送る（ローカルモードでは LocalReferee が条件を判定）。
- **人間の宣言**：宣言ボタンは GameCore が宣言条件（§6.6）を満たすと判定したときだけ有効。条件を満たさない宣言は送らない（不成立の宣言は負けになるため）。

---

## 8. 設定ファイル例

### config.example.toml

```toml
[ui]
host = "127.0.0.1"
port = 8765
notify_my_turn = true          # MANUAL の自手番で音・振動（v1）

[ui.lan]                       # スマホから操作（v1）
enabled = false                # true で 0.0.0.0 にバインド、トークン必須
token = ""                     # 空なら起動時に自動生成

[engine.main]                  # Windows .exe の例
path = 'C:\shogi\YaneuraOu\YaneuraOu_NNUE.exe'    # /mnt/c/... 形式でも可
cwd  = 'C:\shogi\YaneuraOu'                         # 省略時は path のフォルダ
kind = "auto"                  # auto / linux / windows
ponder = true
[engine.main.options]          # 例。エンジンに合わせて書く
Threads = 8
USI_Hash = 1024
NetworkDelay = 120             # [time] margin_ms と同じ目的の余裕。二重に効くので両方を大きくしすぎない
NetworkDelay2 = 1120

[engine.sub]                   # Linux ビルドの例（ローカル対局の相手など）
path = "~/engines/other/engine"
ponder = false

[csa]
host = "wdoor.c.u-tokyo.ac.jp"
port = 4081
accounts_file = "accounts.toml"
default_account = "test1"      # ヘッドレスで --account を省いたときに使う。UI の接続画面では最初の既定（以後は最後に使った名前）
engine = "main"
extended_mode = false          # true = LOGIN に x1 を付ける。LOGIN だけでは対局待ちにならず、通信欄の %%GAME などで申し込む（§6.5、§6.11）。UI でも切り替えられる
send_engine_comment = true     # エンジンの手に '* 評価値 読み筋 を付ける（extended_mode とは無関係）
comment_score_pov = "black"    # black = 先手視点（Floodgate の評価値グラフの慣例）/ mover = 指した側視点。手元の棋譜も同じ視点で書く
keepalive_sec = 60             # 下限 30（CSA プロトコルの規約）
continuous = false             # true: 終局後に自動で再 LOGIN して次の対局を待つ（UI・`--continuous` でも切り替えられる）
rejoin_delay_sec = 10          # 連続参加で、終局から再 LOGIN までの間隔

[local]
black = "main"
white = "sub"
total_sec = 300
increment_sec = 10
byoyomi_sec = 0
max_moves = 256
nyugyoku_rule = "27"           # "27" / "24"（24 点法: 31 点以上で勝ち、24〜30 点の宣言は持将棋）。エンジン側の設定も揃える

[record]
dir = "records"
mark_human_moves = true        # 手元の棋譜で人間の手に印を付ける
formats = ["csa"]              # "csa" / "kif"（Shift_JIS の .kif）/ "kifu"（UTF-8 の .kifu）。複数指定できる

[time]
margin_ms = 1000

[time_guard]
min_remaining_ms = 15000       # 秒読み・加算がこの値以下の対局では MANUAL がほぼ使えなくなる。起動時に警告する（§12）
action = "auto"                # auto / warn

[hint]                         # v1
enabled = false
multipv = 1                    # 2 以上なら対局中に 1 と切り替えられる
send_as_comment = false        # true: 人間の手がヒントの候補と一致したら、その評価値・読み筋を付ける（§6.9）

[policy]
on_engine_resign = "resign"    # resign / ask（ask は v1）
on_engine_failure = "restart"  # restart / manual
stop_grace_ms = 3000
usi_timeout_ms = 30000
quit_timeout_ms = 3000         # quit / stdin クローズ / kill の各段階の待ち時間
max_restarts = 3               # 1 局あたりのエンジン再起動の上限
usi_line_limit_bytes = 1048576 # エンジン出力 1 行の上限

# v1
# [[schedule]]
# plies = "1-8"
# mode = "manual"
```

### accounts.example.toml

```toml
[[account]]
alias = "test1"
name  = "Example_human"
game  = "floodgate-300-10F"
trip  = "CHANGE_ME"
memo  = "序盤介入テスト用"

[[account]]
alias = "local"
name  = "tester"
password = "anything"          # ローカル shogi-server など Floodgate 以外
memo  = "ローカルサーバ用"
```

---

## 9. テスト戦略

偽物はすべて `src/takeover/fakes/` に置く（§5.5）。

**偽エンジン `fakes/fake_engine.py`**（子プロセス。`python -m takeover.fakes.fake_engine [引数]`）
cshogi でランダムな合法手を返す USI エンジン。引数で次を切り替えられる：乱数シード、思考時間、ponder 対応、`go infinite` 対応、MultiPV、`stop` への応答遅延、`stop` 無視（ハング）、一定確率のクラッシュ、CRLF 出力、非合法手を返す、`bestmove resign` / `bestmove win` を返す、ponder 中に勝手に bestmove を返す（USI 違反）、起動時に `usiok` を返さない、巨大な `info` 行を出す。EngineController（I2, I5, I6, I9）のテストに使う。

**スクリプト式エンジン `fakes/scripted_engine.py`**（プロセスなし。`engine.base.Engine` の実装）
テストコードが「いつ・どの bestmove イベントを GameCore のキューに入れるか」を直接指示できる偽物。`think()` などの呼び出しを記録し、テスト側の合図（`release_bestmove(search_id, move)` など）でイベントを発行する。GameCore の遷移表とレース（同着）のテストはこちらで行い、到着順をキューへの投入順として決定的に再現する。

**偽 CSA サーバ `fakes/fake_csa_server.py`**
LOGIN、Game_Summary、AGREE/START、`T` 付きの指し手エコー、終局通知、`%TORYO` / `%KACHI`、時間切れ判定の最小実装。時計は §6.4 の shogi-server の規則（加算してから減算、秒未満切り捨て）に合わせる。相手はランダム合法手を指す（遅延・シード指定可）。受信した行（コメントを含む）を記録し、テストから参照できる。手番外に届いた指し手は本物と同じく保留して次の手番に適用する（誤送信をテストで検出できるように、保留が起きたことも記録する）。

**必須シナリオ**
1. ponder ヒット／ミス
2. 思考中の奪取：stop 後の bestmove が破棄され、二重送信が起きない
3. bestmove と奪取の同着：スクリプト式エンジンで、GameCore のキューへ「bestmove → 奪取」「奪取 → bestmove」の両方の順で投入して再現（子プロセスの遅延には頼らない）
4. 途中返却で `btime` が本手番の既経過分だけ減っている
5. ponder 中の終局
6. `stop` 無応答 → 強制終了・再起動 → 対局続行
7. エンジン異常終了 → `restart` / `manual` それぞれ
8. CRLF 出力のエンジン
9. 人間の非合法手・手番外の着手・1手番2回目の着手（2クライアントからの同時着手を含む）を拒否
10. TimeGuard の発動
11. CLAUDE.md の「過去実装で踏んだ不具合」表の各項目
12. 人間の手にサーバ向けコメントが付かない（偽サーバの受信記録で確認）。手元の棋譜は `mark_human_moves` に従う
13. 評価値コメントの符号：手番側視点で送られ、相手コメントは UI で反転表示される
14. 27 点法の宣言判定（成立／点数不足／駒数不足／王手中／玉が敵陣外）と、条件不成立時に宣言ボタンが無効
15. エンジンのパス解決：Linux パス、`/mnt/c/...`、`C:\...` の変換（変換は純関数としてテスト）
16. ヒント探索（v1）：ヒント中の着手・返却・終局で cancel され、ヒントの bestmove がコミットされない
17. LAN 公開（v1）：トークンなし・誤トークンの HTTP / WebSocket 接続が拒否される
18. 古い `ply` の `move` / `resign` / `declare` が拒否される（相手の手を挟んで遅れて届いた着手が、次の手番で合法でも指されない）
19. `Origin` が自分以外の WebSocket 接続、`Host` が想定外の HTTP 要求が拒否される
20. 千日手：同一局面 4 回で成立し、2〜3 回では成立しない。連続王手の千日手は王手側の負け
21. ponder 中に勝手に返ってきた bestmove が破棄され、その後の相手の手で通常の `go` が送られる
22. CSA：手番外に指し手を送らない（偽サーバの保留記録が空）。LOGIN 行のパスワードがログで伏せられている
23. 長い `info` 行（上限超え）で読み取りタスクが止まらない

**ソークテスト**
- ローカルモードで偽エンジン同士を対局させ、ランダムな時刻にランダムに奪取／返却／人間着手（ランダム合法手）／ヒントのオン・オフを注入して 1000 局。
- pytest のマーカー `soak` を付け、通常の `pytest -q` では少数局（既定 6 局、環境変数 `TAKEOVER_SOAK_GAMES` で変更）だけ回す。1000 局は `TAKEOVER_SOAK_GAMES=1000 pytest -m soak` で明示的に実行する（10 分前後）。一部のエンジンには低確率のクラッシュも混ぜ、再起動の経路も通す。乱数シードを局ごとに記録し、失敗した局を再現できるようにする。
- 不変条件：未完了の探索は常に1つ以下／コミットは1手番1手／コミットした手はすべて合法／ヒントの bestmove がコミットされない／終局後に子プロセスが残らない。
- 違反したらその局のイベントログを保存して失敗させる。

**実環境での確認**（ユーザー立ち会い時のみ）
- Windows .exe と Linux ビルドの実エンジンそれぞれで起動・終了（Windows 側にプロセスが残らないか）
- ローカル shogi-server（Floodgate と同系の Ruby 製サーバ）に実エンジンで接続
- 最後に Floodgate

---

## 10. マイルストーンと完了条件

| M | 内容 | 完了条件 |
| --- | --- | --- |
| M0 | 雛形（v0.3 で配置済み）、設定・名前ストック読込、偽エンジン、スクリプト式エンジン、偽 CSA サーバ、行の解析・生成の純関数（`usi_parse` / `csa_parse`） | `pytest` 緑。偽エンジンが `usi` / `isready` / `go` / `stop` / `quit` に応答する |
| M1 | EngineController（Linux / Windows 起動を含む） | §9 シナリオ 1, 2, 5〜8, 15, 21, 23 と過去不具合表がテストで緑 |
| M2 | GameCore＋時計＋LocalReferee（27点法）＋CLI | ヘッドレスで偽エンジン同士が終局まで指せる。標準入力のコマンド（例：`take b` / `release b` / `move 7g7f` / `declare b`）で奪取・返却・着手・宣言ができる。シナリオ 3, 4, 9, 14, 18, 20 が緑 |
| M3 | CsaTransport | 偽 CSA サーバで AUTO 対局が終局まで進む。`--account` で名前を選べる。シナリオ 12, 13, 22 が緑。続いてローカル shogi-server で実エンジン（ユーザー立ち会い） |
| M4 | Web UI（PC） | ローカルモードで、ブラウザから観戦・奪取・着手・返却・投了・宣言ができる。シナリオ 19 が緑 |
| M5 | CSA モードでの切替、TimeGuard、ソークテスト | §9 シナリオ 1〜15, 18〜23 とソークテストが緑 |
| M6 | LAN 公開・スマホ対応（v1） | スマホのブラウザから観戦・奪取・着手・返却ができる。シナリオ 17 が緑。PC とスマホの同時接続で表示が一致する |
| M7 | 名前ストックの UI・連続参加・KIF 出力（v1） | UI で名前を選んで接続できる。終局後に自動で次局待ちに入る |
| M8 | ヒント表示・予約切替・エンジン投了時の ask（v1） | シナリオ 16 とヒント込みのソークテストが緑 |
| M9 | 24 点法（v1） | 設定で 24 点法に切り替えられ、判定テストが緑 |

---

## 11. 運用上の注意

- **名前**：固定のアカウント名は使わず、名前ストックから選ぶ（§6.10）。なお Floodgate には、人間が指す場合は名前に `_human` を付ける慣習がある。人間が介入する対局に使う名前を登録するときの参考にする。
- **持ち時間**：人間が指すと時間を使う。Floodgate の短い持ち時間（例：300秒＋10秒）では TimeGuard を必ず有効にする。
- **Windows .exe のエンジン**：WSL から `/mnt/c/.../engine.exe` を直接起動できる（設定には `C:\...` 形式で書いてもよい）。評価関数などの相対パスを解決するため `cwd` をエンジンのフォルダにする。出力は CRLF。終了後に Windows 側にプロセスが残っていないかタスクマネージャーで確認する（§12）。
- **Linux ビルドのエンジン**：パイプまわりの問題が少なく、WSL 内ではこちらの方が安定しやすい。
- **UI の開き方**：WSL 内でアプリを起動し、Windows のブラウザで `http://localhost:8765` を開く。
- **スマホから使う場合（WSL2 の注意）**：WSL2 は既定では LAN から直接見えない。次のどちらかが必要（§12）。
  - WSL のミラーモードネットワーク（`.wslconfig` に `networkingMode=mirrored`。Windows 11 の比較的新しいビルドが必要）
  - Windows 側でポート転送（`netsh interface portproxy`）を設定
  - **採用する方式（2026-11-16、ユーザーの希望「必要なときだけ開ける」）**：ポート転送＋ファイアウォール規則。`scripts/takeover-lan.ps1` の `open` / `close` / `status` で追加・削除・確認する（`open` / `close` は管理者の PowerShell で実行）。規則は TCP のそのポートだけ、同じサブネットからだけ、既定では「プライベート」のネットワークでだけ通す。WSL の IP は再起動で変わるので、使うたびに `open` をやり直す。アプリ側は `[ui.lan] enabled = true` が別に必要で、`false` の間は Windows 側が開いていても外から届かない。
  - どちらの場合も Windows ファイアウォールで UI のポートを許可する。家庭内 LAN でのみ使い、ルーターでポートを開けない。

---

## 12. 未決事項

### 決定済み（2026-11-10）

| 項目 | 決定 | 反映先 |
| --- | --- | --- |
| エンジンは Windows .exe か Linux ビルドか | 両方に対応する | §4, §6.3, §8, §11 |
| Floodgate のアカウント名 | 固定名は使わない。複数の名前をストックして選ぶ | §6.10, §8 |
| 人間の手であることを棋譜コメントに残すか | サーバ側には送らない。手元のログ・棋譜に残すオプションを付ける | §6.5, §6.8 |
| 拡張モードの評価値コメントの符号 | 手番側（指した側）の有利不利で判断する | §6.5 |
| 入玉宣言の点数法 | 27 点法を優先。24 点法は後で対応 | §6.6, §10 |
| MANUAL 中のヒント表示 | オプションで表示できるようにする | §6.9, §10 |
| スマホから操作する LAN 公開 | 必要 | §6.7, §10, §11 |

### v0.3 の再精査で確定したこと（2026-11-12。根拠は `docs/SPEC_REVIEW.md`）

| 項目 | 結論 | 反映先 |
| --- | --- | --- |
| フィッシャー加算のタイミングと `T` の丸め | 加算してから減算（その手の加算分はその手に使える）。`T` は秒未満切り捨て。shogi-server のミラーのソースで確認。Floodgate 本番との差は実ログで検算（下の未決に残す） | §6.4 |
| キープアライブ間隔 | 既定 60 秒、下限 30 秒 | §6.5, §8 |
| LOGIN の ` x1` | 既定で付けない。付けると自動マッチ登録が効かない。コメント送信に x1 は不要 | §6.5, §8 |
| 相手の評価値コメント | 対局中は中継されない前提で作る | §6.5 |
| 入玉宣言の判定 | cshogi の `is_nyugyoku` / `nyugyoku_result` を使う | §6.6 |
| 千日手の判定 | cshogi の `is_draw` は 2 回目で成立するので使わず、4 回を自前で数える | §6.6 |
| 偽物の置き場所 | `src/takeover/fakes/`。プロセスなしのスクリプト式エンジンを追加 | §5.5, §9 |
| UI コマンドの形 | `color` と `ply` を持たせ、古い画面からのコマンドを拒否 | §6.7 |
| ローカル接続の保護 | v0 から WebSocket の `Origin` と HTTP の `Host` を検査 | §6.7 |
| `cancel()` と再起動 | `cancel()` は再起動を待たない。再起動は裏で行いイベントで通知。回数上限あり | §6.3 |

### §7（切替の仕様）に関わる確認事項 — 2026-11-14 に仮決め案で進めることを承認済み

ユーザーの承認により、下の「仮決め案」を当面の仕様として実装する（§7 の本文と食い違う場合はこの表が優先）。動くものができてから見直す。評価値コメントの符号（`csa.comment_score_pov`）と `csa.extended_mode` も、削除せず設定で切り替えられる形のまま進める。

| # | 問題 | 仮決め案 |
| --- | --- | --- |
| Q1 | §7.1 に「相手手番・AUTO・待機（ponder なし）で奪取」の行がない | MANUAL にするだけ（エンジンは何もしていないので操作不要） |
| Q2 | エンジンが使えない席（人間専用席、エンジンが DEAD・再起動中）での返却が未定義 | 人間専用席・DEAD は返却を拒否して通知。再起動中は AUTO にして、`EngineReady` で `go` |
| Q3 | エンジン再起動中の奪取が未定義 | 即 MANUAL。再起動は裏で続ける |
| Q4 | TimeGuard が AUTO に戻した直後に再奪取すると、即座にまた TimeGuard が発動して往復する | `action = "auto"` のとき、閾値未満の間は奪取を拒否して通知する |
| Q5 | TimeGuard の式「残り時間＋今回使える加算・秒読み」に本手番の既経過が入るか曖昧。また秒読み（や加算）が閾値以下だと、持ち時間を使い切った後は MANUAL が一切使えない（例：秒読み 10 秒・閾値 15 秒） | 式は「残り時間 − 本手番の既経過 ＋ 秒読み」（フィッシャーは加算込みの残り時間）。起動時・対局開始時に、秒読み／加算が閾値以下なら警告を出す。閾値の自動調整はしない |
| Q6 | CSA モードで、自分の手を送った直後に ponder を始めるか、エコー（`T`）を待ってから始めるか。直後だと時計が未確定のまま `go ponder` の時間引数を作ることになる | エコーを受け取って時計を確定させてから ponder を開始する（遅れは通信 1 往復分） |
| Q7 | 古い画面から遅れて届いた着手（§7.2 は「相手の手が来るまで拒否」としか書いていない） | コマンドに `ply` を持たせ、不一致なら拒否（§6.7 に反映済み。§7.2 への追記のみ承認待ち） |
| Q8 | エンジンの `bestmove win`：§7.4 は「`%KACHI` を送る」、CLAUDE.md は「宣言条件を満たさない状態で `%KACHI` を送らない」 | CSA モードでも GameCore が条件を事前判定する。満たさなければ送らず、「非合法な bestmove」と同じエンジン異常として扱う |
| Q9 | ponder 中に USI 違反の bestmove が来た場合 | 破棄して IDLE。次の相手の手で通常の `go`（§6.3 に反映済み。§7.1 への行の追加のみ承認待ち） |

### 未決

- [x] **評価値コメントの符号** → 先手視点に決定（2026-11-16。Floodgate の評価値グラフは先手視点で、指した側視点で送ると後手番のときに反転することを実対局でユーザーが確認）。`csa.comment_score_pov` の既定を `black` に変更（v0.4.0）。
- [ ] Floodgate 本番の時計が §6.4 の規則（ミラーのソース）と一致するか。`Time_Roundup` の有無。実対局のログで検算するテストを用意する。→ 2026-11-16 の 1 局（`floodgate-300-10F`）では Game_Summary に `Time_Roundup` の行はなく、時間切れも起きなかった。ただし残り時間を使い切る局面はなかったので、時間切れ間際の規則は未検算。
- [ ] エンジンの思考時間に渡す `btime` に、その手の加算分を含めるか（サーバは加算分をその手に使えるが、USI の `binc` をエンジンがどう解釈するかはエンジン依存）。安全側として含めない（`btime` = 加算前の残り）で始める。
- [ ] `score mate` を評価値コメントに写像するときの具体値（既存クライアントの慣例を確認）
- [x] WSL から起動した Windows .exe を kill したとき、Windows 側にプロセスが残るか → 2026-11-16 の確認では残らなかった（`quit` での終了、WSL 側プロセスへの `kill -9` のどちらも。`tasklist.exe` で確認。変更履歴 v0.3.5）。別のエンジンで残る例が出たら対処する
- [x] スマホ接続の方式：WSL のミラーモードが使えるか、ポート転送にするか（ユーザーの Windows ビルドによる）（未確認のまま。スマホ向けレイアウトは v0.4.0 で入れたが、確認は PC のブラウザで幅 390px にした表示だけで、実機では未確認） → 方式はポート転送に決定（§11。`scripts/takeover-lan.ps1`、手順は `scripts/README.md`）。2026-11-16 にユーザーが `open` → アプリ起動 → スマホ実機からの操作 → 終了・`close` まで実施して確認。
- [ ] QR コード表示（スマホで URL を開きやすくする）を入れるか。依存追加なしでできるか

---

## 変更履歴

- v0.5.1（2026-11-17）：通信欄を Floodgate で試した結果（ユーザーによる確認）を反映。
  - コメントの `*` の数：入力した形がそのままサーバの棋譜に残るよう、1 つ目のコメントは `*` を 1 つ減らして送る（§6.11「コメントの形」）。手元の棋譜も入力した形のまま残す。
  - コメントを複数行付けられるようにした（続けて入力。8 行まで）。サーバ側で 2 行目以降が残るかは未確認（公開ソースでは残らない）。
  - 拡張モード（x1）でも、LOGIN 後に名前ストックのゲーム名で `%%GAME <ゲーム名> *` を自動で送る。LOGIN のパスワード欄は識別文字列だけにした（§6.5）。
  - プルダウンから `%HIKIWAKE`、`%%SHOW`、`%%RATING` を外した（HTML のコメントにして残してある）。
  - 偽 CSA サーバに、本物と同じ規則で棋譜のコメントを作る記録（`kifu_comments`）を追加。
- v0.5.0（2026-11-16）：サーバとの通信欄（§6.11）と拡張モード（x1）を追加（ユーザーの要望）。
  - UI の下部に、通信ログ・サーバへ送る 1 行の入力欄・よく使うコマンドのプルダウンを追加。接続パネルに「拡張モード（x1）」のチェックを追加。
  - `csa.extended_mode = true` を実装（これまでは起動時エラー）。`##` で始まる応答行を終局理由として扱わないようにした。
  - 人間の手に自由コメントを付けられるようにした（`MoveCommand.comment`、`MoveRecord.comment`）。サーバへ送る人間の手にコメントが付く 2 つ目の例外（§6.5）。
  - `%X` を送る `RawSpecialCommand` を追加。§7 の遷移表・レース規則は変えていない（投了・宣言と同じ検査を通る、1 手番 1 回のコミット）。
  - 偽 CSA サーバを本物に合わせた：拡張コマンドには `##[…] +OK` を返すだけで対局に影響しない、未知の行には `##[ERROR]`、未知の `%X` は非合法手、x1 の LOGIN では `%%GAME` / `%%CHALLENGE` を待つ。
  - 確認：偽 CSA サーバと実際のブラウザ（PC 幅・390px 幅）で、通信ログの表示、`%%WHO` / `%%LIST` の送受信、指し手の入力の拒否を確認。x1 での対局申し込みは自動テストで確認。実サーバでは未確認。
- v0.4.3（2026-11-16）：スマホから開くための Windows 側の設定スクリプト `scripts/takeover-lan.ps1` を追加（§11）。アプリのコードは変更なし。 手順書 `scripts/README.md` を追加。スマホ実機からの操作はユーザーが確認した。
- v0.4.2（2026-11-16）：ローカル対局の「先後を入れ替えた連続対局」を実装（§6.6、§8）。
  - `app.py` のローカル対局の実行部を、複数局を回す形（`_run_games`）に書き直した。1 局のとき（既定）の動きは変わらない。
  - 確認：偽エンジンで 3 局（先後の入れ替え・通算成績・1 局ごとの棋譜・子プロセスが残らない）、入れ替えなしの 2 局、途中の局でのエンジン起動失敗。実エンジン（MaterialLv1 どうし、UI あり）で 3 局を通し、先後の入れ替えと棋譜 3 局ぶん（CSA / KIF）を確認（3 局とも最大手数での引き分けだったので、勝敗の集計は偽エンジンのテストでの確認）。
- v0.4.1（2026-11-16）：M7 を Floodgate で確認し、見つかった不具合を 1 件修正。
  - 確認できたこと（`floodgate-300-10F`）：接続画面で名前を選んで接続、連続参加（終局 → 棋譜保存 → `LOGOUT` → 10 秒後に再 LOGIN → 次のマッチングで 2 局目が成立）、対局中に連続参加をオフ → 終局後は接続画面に戻り再接続しない、UI からの名前の追加・編集、追加した名前での接続と対局、対局待ちの「待機をやめる」（`LOGOUT` して接続画面へ。棋譜は作らない）。名前の追加以降はユーザーによる確認。送信失敗・手番外の送信・エンジンの再起動はなし。UI から入力された識別文字列は、イベントログにもコンソール出力にも出ていない。
  - 評価値コメントの先手視点：後手番の対局で、エンジンの値（指した側視点）の符号を反転した値が送られていることをログで確認。
  - **不具合と修正**：ヒント探索中に終局すると、ヒント用に変えた MultiPV が戻されないままだった。1 起動 1 局のときは害がなかったが、連続参加ではエンジンを使い回すので、次の対局を MultiPV 3 のまま指していた（2 局目で発生）。終局時に MultiPV を 1 に戻すようにした（`gameover` の前）。テストを追加し、実エンジン（MaterialLv1）と偽 CSA サーバの 2 局連続で、2 局目が MultiPV 1 になることを確認。
- v0.4.0（2026-11-16）：M7（名前ストックの UI・連続参加・KIF 出力）、M9（24 点法）、スマホ向けレイアウト。評価値コメントを先手視点に変更。
  - **評価値コメント**：`csa.comment_score_pov` の既定を `mover` から `black`（先手視点）へ（§6.5、§12）。手元の棋譜も同じ視点で書く。既存の `config.toml` にこのキーを書いていなければ、次の起動から先手視点になる。
  - **M7**：`session.py`（CsaSession）を追加。接続画面・名前ストックの追加／編集／削除・連続参加・対局待ちの切断（§6.5、§6.7、§6.10）。`accounts.py` に書き出しを実装。KIF 出力（§6.8）。`LoggedIn` イベントを追加し、LOGIN 後のフェーズを WAITING_GAME にした。
  - **M9**：24 点法では引き分けになる宣言も有効にした（§6.6）。スナップショットに `declare_result` を追加。
  - **UI**：接続パネル、名前ストックのダイアログ、スマホ用の下部ボタンバー、手番の通知のオン／オフ、対局前の初期配置の表示（§6.7）。
  - 起動の変更：`python -m takeover csa` を `--account` なしで起動すると、すぐには接続せず接続画面で待つ（以前は `default_account` ですぐ接続していた）。
  - ブラウザを繋いだまま Ctrl-C で止めたときのトレースバック（v0.3.6 の既知の問題）を出ないようにした。
  - 確認：偽 CSA サーバと偽エンジンで、接続画面 → 接続 → 対局 → 連続参加で 2 局目、を実際のブラウザ（Edge のヘッドレス、PC 幅と 390px 幅）で表示して確かめた。実サーバ（Floodgate）とスマホ実機では未確認。
  - 未実装：ローカル対局の「先後を入れ替えた連続対局」（§6.6）→ v0.4.2 で実装。依存の追加なし。
- v0.3.8（2026-11-16）：Floodgate の 3 局目で、`hint.send_as_comment` と TimeGuard による奪取の拒否を確認（コードの変更なし）。
  - `floodgate-300-10F`、後手、123 手で負け（エンジンの投了）。時間切れ・非合法手・手番外の送信・送信失敗はなし。
  - `hint.send_as_comment = true`：人間の手 6 手のうち、その時点のヒントの候補（3 つ）と一致した 4 手に候補の評価値・読み筋が付き、一致しなかった 2 手はコメントなしで送られた。2 位・3 位の候補と一致した手にも、その候補の値が付いた。
  - TimeGuard：自動返却 3 回、奪取の拒否 1 回（「残り時間が少ないため奪取できません」）。自動返却の直後の再奪取は、エンジンが指して加算 10 秒が入ると式（残り − 経過 ＋ 加算）が閾値 15 秒を上回るので受け付けられ、数秒後にまた自動返却になる。拒否が出るのは表示の残り時間が約 5 秒未満の間だけ。閾値が加算以下に近いと Q4 の「往復の防止」は狭い範囲でしか効かない（閾値を加算＋表示で残したい秒数にすれば広がる）。
- v0.3.7（2026-11-16）：Floodgate で、人間が UI から操作する対局の 2 局目を確認（コードの変更なし）。
  - `floodgate-300-10F`、先手、158 手で勝ち（人間の入玉宣言 `%KACHI` → サーバが `#JISHOGI` `#WIN`）。宣言が実サーバで受理されたのは初めて。
  - 人間の手 64（すべてコメントなしで送信）、エンジンの手 16（コメント付き）。切替は奪取 5 回・返却 4 回：自手番での奪取、相手の手番での奪取（「次の手番から有効」の通知 4 回）、ボタンでの返却、奪取の 2 秒後に着手せず返却、を含む。成・不成の選択と持ち駒を打つ手も通った。UI の接続が 3 回替わっても（再読み込みなど）操作は続けられた。
  - 時間切れ・非合法手・手番外の送信・エンジンの再起動・送信失敗はなし。終局と同時に棋譜保存と `LOGOUT`。
- v0.3.6（2026-11-16）：Floodgate で、人間が UI から操作する対局を 1 局確認（コードの変更なし）。
  - `floodgate-300-10F`、後手、79 手で負け（エンジンの投了）。思考中の奪取（76 手目）→ 人間の着手 2 手（コメントなしで送信）→ 80 手目の手番で TimeGuard が自動返却 → エンジンが投了、の順に動いた。時間切れ・非合法手・手番外の送信はなし。終局と同時に棋譜保存と `LOGOUT` が行われた（v0.3.5 の修正の確認）。
  - TimeGuard は、UI の残り時間の表示が約 5 秒のときに発動した。式（§12 Q5）が「表示の残り時間 − 本手番の経過 ＋ この手で使える加算 10 秒」で、これが閾値 15 秒を下回った時点だから。shogi-server の `time_clock.rb` の時間切れ判定（`mytime - t + byoyomi + fischer <= 0`）と同じ数え方で、式は正しい。ただし UI の表示だけを見ると閾値より遅く発動したように見える（表示の改善は未着手）。
  - 既知の見た目の問題：ブラウザを繋いだまま Ctrl-C で止めると、uvicorn が WebSocket ハンドラの `CancelledError` をトレースバックとして出す（終了処理そのものは正常。エンジンは `quit` で終了）。
- v0.3.5（2026-11-16）：Windows .exe のエンジンでの確認と、ヒントまわりのオプション追加（ユーザーの要望）。
  - Windows .exe（`C:\Shogi\Tuyouraou\YaneuraOu_SFNN_halfka2_1024_7_64_k3k3-V990DEV_AVX2.exe` と `C:\Shogi\NAGISA_V4-Windows-AVX2\NAGISA_V4.exe`、Threads 2・USI_Hash 1024）：`C:\...` 形式と `/mnt/c/...` 形式のどちらのパスでも起動でき、ローカル対局で §7.1 の切替一式（思考中・ponder 中の奪取、返却、連打、両席 MANUAL、投了）と、`kill -9` からの再起動が Linux ビルドと同じに動いた。出力の CR は除去されている。終了後・強制終了後とも Windows 側にプロセスは残らなかった（§12）。
  - 追加：ヒントの候補数の切替（`set_hint_multipv`、`hint multi on|off`）と、ヒントの読み筋を人間の手のコメントにする設定 `hint.send_as_comment`（§6.5、§6.9、§8）。後者は「人間の手にはサーバ向けコメントを付けない」（2026-11-10 の決定）の例外で、既定はオフ。実エンジン＋偽 CSA サーバで、候補と一致した手にだけコメントが付くことを確認。
  - Floodgate への接続（MaterialLv1、`floodgate-300-10F`、UI 付き、人間の介入なし）：LOGIN → マッチング → 対局 → 終局（66 手、相手の投了で勝ち）まで問題なく進んだ。
    - 時計：全 66 手で、サーバの `T` から計算した残り時間で `go` を送り、時間切れ・非合法手・手番外の送信はなし。`ponderhit` 8 回、ponder ミス 24 回とも正常。
    - 相手の評価値コメントは 1 行も中継されなかった（§6.5 の前提どおり）。
    - Game_Summary は `Max_Moves:512`、`Least_Time_Per_Move:0`、`Time_Roundup` の行はなし。
  - Floodgate での確認で見つけて直した点：
    - 終局後、UI を閲覧用に開いている間は棋譜が保存されず、サーバとの接続も閉じていなかった（サーバ側が切った後も 1 分ごとにキープアライブを送り続け、送信失敗をログに出し続けた）。終局したらすぐに棋譜保存 → `LOGOUT` → 切断を行い、UI だけを残す。送信に一度失敗したらキープアライブを止める。
    - 棋譜の `$START_TIME` がアプリの起動時刻だった。対局開始の時刻にした。
  - 気づいた点（変更なし）：CSA モードではエンジンのログの `color` が、席の色に関係なく `b` になる（色が Game_Summary で決まる前にエンジンを作るため）。ログを読むときに紛らわしい。

- v0.3.4（2026-11-16）：Linux ビルドの実エンジン（やねうら王 MaterialLv1 9.80、`~/work/YaneuraOu/YO980_Material1`）での確認。仕様の変更なし。
  - 確認できたこと（いずれも実エンジンのプロセスが残らずに終了）：
    - ローカル対局（ヘッドレス）：エンジン同士で終局まで（175 手、詰み）。`go` の多重送信なし、`ponderhit` 46 回・ponder ミス 41 回とも正常、`stop` への bestmove は即時に返り破棄される（I5、I6）。
    - §7.1 の遷移：思考中の奪取、ponder 中の奪取、自手番・相手手番での返却、非合法手の拒否、両席同時 MANUAL、奪取・返却の連打、人間の投了。
    - TimeGuard（秒読み 30 秒＋5 秒、閾値 15 秒）：自動返却 → エンジンが指し継ぐ → 直後の再奪取は拒否（Q4）。`go ... byoyomi` の引数も §6.4 の式どおり。
    - Web UI：WebSocket 2 接続で表示が一致、古い `ply` の着手の拒否、MultiPV 3 のヒント、別の接続からの着手、返却、投了。
    - CSA モード（偽 CSA サーバ）：LOGIN（ログではパスワードを伏せる）→ 対局 → 奪取 → 人間の手はコメントなしで送信 → 返却 → 終局。時間引数はサーバの `T` と一致。
    - エンジン異常：思考中・ponder 中の `kill -9` から再起動して指し継ぐ。
  - 見つけて直した不具合：
    - ヘッドレスの標準入力：複数行が一度に届くと 1 行目しか処理されなかった（`readline()` のバッファに残る）。`os.read` で読んで自前で行に分ける（`app.split_stdin_lines`）。
    - 片付け中に SIGINT / SIGTERM が来ると、シグナルハンドラを外した後なので既定動作で落ち、エンジンの `quit` → wait を飛ばしていた。片付けが終わるまでシグナルを無視する。
    - ログの `engine_exit` の `killed` が常に true だった。実際に kill したときだけ true にする。
  - 気づいた点（変更なし）：
    - 相手手番での奪取は、常に「送信済みのため、奪取は次の手番から有効です」と通知される（直前に自分の手を指しているため）。害はないが、うるさければ文言・条件を見直す。
    - やねうら王は返却直後の 1 手に 64 秒使うことがあった（300 秒＋10 秒、残り 313 秒）。探索は最後まで続いており、エンジン自身の時間配分によるもの。
    - 相手が即指しのとき、自分の手のエコーと相手の手が同時に届くと、ponder を開始してすぐ `stop` する（Q6 の手順どおり。動作は正しいが無駄な 1 往復）。
  - 開発環境：`python3-venv` がない環境では `uv venv .venv && uv pip install -e ".[dev]"` で代用できる（CLAUDE.md に追記）。
  - まだ確認していないこと：Windows .exe のエンジン、ローカル shogi-server・Floodgate への接続、ブラウザ・スマホ実機での操作感。

- v0.3.3（2026-11-14）：M3〜M5 実装に伴う追記。
  - `MoveConfirmed` に相手の評価値コメント（指した側視点）を載せられるようにした（§6.5。通常は届かない）。
  - UI の評価値の視点は、CSA モードは自分の色、ローカルモードは先手（§6.7）。スナップショットに `view` を追加。
  - 接続時に `hello` メッセージ（通知設定、ループバック接続に限りスマホ用 URL）を送る（§6.7）。
  - `Host` / `Origin` の規則：localhost・IP アドレスの直指定・`ui.lan.allowed_hosts` のいずれかだけを許可。`Origin` は `Host` と一致すること（§6.7）。
  - LAN 公開時のトークン認証（M6 の一部）を M4 と同時に実装した。スマホ実機での確認と WSL2 のネットワーク設定は未実施（§11、§12）。
  - TimeGuard の閾値が 1 手あたりの秒読み・加算以上のとき、対局開始時に通知を出す（Q5）。
  - ソークテストの通常時の局数を 6 に変更（§9）。

- v0.3.2（2026-11-14）：M1・M2 実装に伴う明確化。
  - LocalReferee を「プロセス内の CSA サーバ役」とし、消費時間の計測・時間切れの検出・`MoveConfirmed` / `GameEnded` の発行を任せる（§6.1）。GameCore は両モードとも「コミット → 進行役の確認（エコー）→ 次の手番開始」の同じ流れで動く。
  - 探索の管理は席ごと（`expected_search_id` / `hint_search_id` は LocalSeat 1 つにつき 1 組）。ローカルモードは両席にエンジンがあるため。
  - 非合法な bestmove・条件不成立の `bestmove win` は、`on_engine_failure = "restart"` なら同じ手番で 1 回だけ考え直させ、再発したら MANUAL にする（プロセスの再起動はしない）。
  - エンジンの info をイベントにする最小間隔 `policy.engine_info_interval_ms`（既定 200）を追加。
  - core から純関数モジュール `engine.usi_parse` を参照してよいことにした（§5.5）。

- v0.3.1（2026-11-14）：Q1〜Q9 を仮決め案で進めることを承認（§12）。M0 実装。`core/rules.py`（千日手の回数判定。LocalReferee と偽 CSA サーバで共用）を追加。`score mate N` のコメント値は仮に ±(100000 − N) とした（`csa_parse.MATE_SCORE`）。

- v0.3（2026-11-12）：仕様の再精査とディレクトリ構成の確定。
  - 構成：偽物を `src/takeover/fakes/` へ移動、スクリプト式エンジンを追加、`engine/base.py`・`transport/base.py`（インターフェース）、`usi_parse.py`・`csa_parse.py`・`ui/messages.py`（純関数）、`core/types.py`・`core/snapshot.py`、`app.py` を追加（§5.5）。
  - 事実確認による修正：LOGIN の `x1` を既定オフに（§6.5）、shogi-server の時計規則を明記（§6.4）、相手コメントは中継されない（§6.5）、cshogi の千日手判定は使えない・入玉判定は使える（§6.6）、キープアライブ既定 60 秒（§6.5）。
  - 欠けていた規定の追加：タイマーのイベント化・時刻の注入・CSA モードの盤面反映タイミング・reconcile 指針（§6.1）、ponder 中の違反 bestmove・`cancel()` と再起動の分離・再起動上限・行長上限・`quit` 手順（§6.3）、秒読みへの食い込み（§6.4）、Game_Summary の項目・REJECT・終了処理（§6.5）、UI コマンドの `color` / `ply`・Origin 検査（§6.7）、ログのマスク（§6.8）。
  - テスト：シナリオ 18〜23 を追加、ソークテストのマーカー化（§9）。マイルストーンのシナリオ割り当てを更新（§10）。
  - §7 は未変更。確認事項 Q1〜Q9 を §12 に記載（承認待ち）。
  - 依存の追加なし（HTTP / WebSocket のテストは `uvicorn[standard]` に含まれる `websockets` を使う）。
- v0.2（2026-11-10）：未決事項への回答を反映。エンジン両対応（起動・パス変換・終了処理）、名前ストック（§6.10）、人間の手の記録をサーバに送らない方針、評価値コメントの符号規約、27 点法の宣言判定と人間の宣言ボタンの事前チェック、ヒント表示（§6.9、I11）、LAN 公開とスマホ対応を v1 の必須に。マイルストーンを M9 まで再編。
- v0.1：初版ドラフト
