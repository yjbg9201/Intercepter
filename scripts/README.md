# scripts — スマホから UI を開くための手順

同じ LAN のスマホから shogi-takeover の UI を開くための、Windows 側の設定スクリプトと使い方です。
対象は WSL2 の既定の構成（NAT）です。方式を決めた経緯は `docs/SPEC.md` §11 にあります。

## 仕組み

スマホからの通信は、次の 3 か所を順に通ります。3 つとも開いているときだけ繋がり、どれか 1 つを閉じれば届きません。

| 関門 | 開ける | 閉じる |
| --- | --- | --- |
| Windows のファイアウォール | `takeover-lan.ps1 open` | `takeover-lan.ps1 close` |
| Windows → WSL のポート転送 | 同上（同じコマンドで両方入る） | 同上 |
| アプリ | `config.toml` の `[ui.lan] enabled = true` | `false` に戻す（既定） |

ファイアウォールの規則は、TCP のポート 8765 だけを、同じ LAN 内の端末からだけ通します。
既定では、Windows 上でネットワークの種類が「プライベート」のときにだけ有効です。

## スマホで試すときの流れ

1. **Wi-Fi を「プライベート」にする。**
   設定 → ネットワークとインターネット → Wi-Fi → 接続中のネットワークのプロパティ → ネットワーク プロファイルの種類。
   変更はすぐ効きます（再起動は不要）。
2. **`config.toml` で LAN 公開をオンにする。**

   ```toml
   [ui.lan]
   enabled = true
   token = ""        # 空なら起動のたびにランダムなトークンを生成。固定したいときは長めの英数字を入れる
   ```

3. **Windows 側を開ける。** PowerShell を「管理者として実行」で開いて:

   ```powershell
   cd \\wsl.localhost\Ubuntu-24.04\home\hiro\work\Intercepter\scripts
   powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 open
   ```

   スマホで開く URL（`http://192.168.x.x:8765/?token=<トークン>` の形）が表示されます。
4. **アプリを起動する。** WSL（Ubuntu）のターミナルで:

   ```bash
   cd ~/work/Intercepter
   source .venv/bin/activate          # 先頭にドットが付く（.venv）。成功するとプロンプトに (.venv) が出る
   python -m takeover local --config config.toml     # ローカル対局（エンジンどうし）
   # Floodgate などに繋ぐとき: python -m takeover csa --config config.toml（接続画面で止まり、「接続」を押すまで繋がない）
   ```

   `activate` を使わずに `.venv/bin/python -m takeover local --config config.toml` でも同じです。
5. **スマホで開く。** 手順 3 で表示された URL の `<トークン>` を、アプリの起動時にターミナルへ出たトークンに置き換えて開きます（下の「アドレスの注意」を参照）。
6. **終わったら閉じる。**
   - アプリ: 起動したターミナルで Ctrl-C。
   - Windows 側: 管理者の PowerShell で `powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 close`
   - `config.toml` の `enabled` を `false` に戻す。
   - 必要なら Wi-Fi を「パブリック」に戻す。

## アドレスの注意

アプリを起動すると、ターミナルに URL が 2 行出ます。

```
UI: http://localhost:8765/?token=xxxxxxxx
スマホから: http://172.x.x.x:8765/?token=xxxxxxxx
```

- **`172.` で始まるアドレスはスマホでは使えません。** これは WSL 内部のアドレスで、Windows の外からは届きません。
- **スマホでは `192.168.` などで始まる、Windows PC の LAN 側のアドレスを使います。** `takeover-lan.ps1 open` が表示する URL がこれです（`ipconfig` の「IPv4 アドレス」でも確認できます）。
- **トークンは 2 行とも同じです。** スマホの URL にも同じトークンを付けます。`token = ""` のままだと起動のたびに変わります。
- **LAN 公開中は PC のブラウザでもトークンが必要です。** `http://localhost:8765/` だけでは「token required」と出るので、1 行目の URL をそのまま開きます。

## takeover-lan.ps1 の使い方

```powershell
powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 open      # 開ける（管理者）
powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 close     # 閉じる（管理者）
powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 status    # 状態の確認（管理者でなくてよい）
```

| コマンド | すること |
| --- | --- |
| `open` | WSL の IP を調べ、ポート転送とファイアウォール規則を入れ直す。スマホ用の URL を表示する |
| `close` | ポート転送とファイアウォール規則を削除する |
| `status` | 転送・規則・ネットワークの種類を表示する。規則の対象と今の種類が合っていなければ注意を出す |

オプション:

| オプション | 既定 | 意味 |
| --- | --- | --- |
| `-Port` | `8765` | UI のポート。`config.toml` の `[ui] port` と同じ値にする |
| `-NetworkProfile` | `Private` | 規則を有効にするネットワークの種類（`Private` / `Public` / `Any`）。`Public` や `Any` は外出先の Wi-Fi でも開くので、`close` を忘れないこと |
| `-Distro` | 既定のディストリビューション | WSL の IP を調べる対象 |

スクリプトが触るのは、ポート転送 1 件と「shogi-takeover UI (TCP 8765)」という名前の規則 1 件だけです。

## うまく繋がらないとき

- **Windows や WSL を再起動したあと繋がらない**: WSL の IP が変わっています。`open` をやり直します（古い設定は自動で入れ替わります）。
- **スマホで開けない**: `status` で、転送と規則の両方が「あり」か、ネットワークの種類が規則の対象と合っているかを確かめます。スマホが PC と同じ Wi-Fi に繋がっているかも確認します。
- **「token required」と出る**: URL にトークンが付いていないか、アプリを起動し直してトークンが変わっています。
- **「forbidden host」と出る**: IP アドレスではなく名前（PC 名など）で開いています。IP アドレスで開くか、`config.toml` の `[ui.lan] allowed_hosts` にその名前を足します。

## 安全面

- 届くのは同じ LAN 内の端末だけです。ルーターでポート開放をしない限り、インターネットからは届きません（ルーター側は設定しないでください）。
- 通信は暗号化されていません（HTTP）。信頼できる自宅の LAN で使う前提です。
- トークンを知らない端末は、同じ LAN 内でも画面を開けません。
- `enabled = false` の間、アプリは WSL 内の `127.0.0.1` でしか待ち受けません。`close` を忘れても、アプリ側を戻してあれば外からは届きません。
