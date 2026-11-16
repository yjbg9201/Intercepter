<#
.SYNOPSIS
  shogi-takeover の UI を、同じ LAN のスマホから開けるようにする／元に戻す（WSL2 の NAT 構成用）。

.DESCRIPTION
  open   : Windows のポート転送（portproxy）とファイアウォール規則を追加する。管理者権限が必要。
  close  : 上の 2 つを削除する。管理者権限が必要。
  status : いまの状態を表示する（権限不要）。

  このスクリプトが触るのは、Windows のポート転送 1 件と、名前付きのファイアウォール規則 1 件だけ。
  アプリ側は別に、config.toml の [ui.lan] enabled = true が必要（false の間は外から届かない）。
  WSL の IP アドレスは再起動で変わるので、使うたびに open をやり直す。

.EXAMPLE
  管理者の PowerShell で:
    powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 open
    powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 close
  状態の確認（管理者でなくてよい）:
    powershell -ExecutionPolicy Bypass -File .\takeover-lan.ps1 status
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("open", "close", "status")]
    [string]$Action = "status",

    # UI のポート（config.toml の [ui] port と同じ値）
    [int]$Port = 8765,

    # ファイアウォール規則を有効にするネットワークの種類。既定は Private（自宅の LAN を「プライベート」にして使う）
    [ValidateSet("Private", "Public", "Any")]
    [string]$NetworkProfile = "Private",

    # WSL のディストリビューション名（省略時は既定のもの）
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"
$RuleName = "shogi-takeover UI (TCP $Port)"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-WslAddress {
    if ($Distro) { $raw = & wsl.exe -d $Distro hostname -I } else { $raw = & wsl.exe hostname -I }
    $first = ("$raw".Trim() -split "\s+")[0]
    if ($first -notmatch "^\d{1,3}(\.\d{1,3}){3}$") {
        throw "WSL の IP アドレスを取得できません（WSL が起動しているか確認してください）。取得した値: '$raw'"
    }
    return $first
}

function Get-LanAddresses {
    # 既定のゲートウェイを持つ（＝ LAN につながっている）アダプタの IPv4 アドレス。WSL 用の仮想アダプタは除く
    Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" -and $_.InterfaceAlias -notmatch "WSL|vEthernet" } |
        ForEach-Object { $_.IPv4Address.IPAddress }
}

function Get-ProxyLines {
    # このポートの転送設定の行（なければ空）
    $lines = & netsh.exe interface portproxy show v4tov4
    return @($lines | Where-Object { $_ -match "^\s*\S+\s+$Port\s+\S+\s+\d+\s*$" })
}

function Show-Status {
    Write-Host "== shogi-takeover の LAN 公開の状態（ポート $Port）=="
    $proxy = Get-ProxyLines
    if ($proxy.Count) {
        Write-Host "ポート転送      : あり"
        $proxy | ForEach-Object { Write-Host "                  $($_.Trim())" }
    } else {
        Write-Host "ポート転送      : なし"
    }
    $rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Write-Host "ファイアウォール: 規則あり（有効: $($rule.Enabled) / 対象: $($rule.Profile)）"
    } else {
        Write-Host "ファイアウォール: 規則なし"
    }
    $profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue
    foreach ($p in $profiles) {
        Write-Host "ネットワーク    : $($p.Name)（$($p.InterfaceAlias)）= $($p.NetworkCategory)"
    }
    if ($rule -and $profiles) {
        $active = @($profiles | ForEach-Object { "$($_.NetworkCategory)" })
        $target = "$($rule.Profile)"
        if ($target -ne "Any" -and -not ($active | Where-Object { $target -match $_ })) {
            Write-Host "注意: 規則の対象（$target）と、いまのネットワークの種類（$($active -join ', ')）が合っていません。このままではスマホから届きません。" -ForegroundColor Yellow
        }
    }
    if ($proxy.Count -and $rule) {
        Write-Host "→ Windows 側は開いています。アプリが [ui.lan] enabled = true で動いていれば、スマホから届きます。"
    } elseif (-not $proxy.Count -and -not $rule) {
        Write-Host "→ Windows 側は閉じています。スマホからは届きません。"
    } else {
        Write-Host "→ 片方だけ残っています。スマホからは届きません。close で両方消せます。" -ForegroundColor Yellow
    }
}

function Remove-Settings {
    foreach ($line in Get-ProxyLines) {
        $listen = ($line.Trim() -split "\s+")[0]
        & netsh.exe interface portproxy delete v4tov4 listenaddress=$listen listenport=$Port | Out-Null
    }
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

if ($Action -eq "status") {
    Show-Status
    exit 0
}

if (-not (Test-Admin)) {
    Write-Host "open / close には管理者権限が必要です。PowerShell を「管理者として実行」で開き直してください。" -ForegroundColor Red
    exit 1
}

if ($Action -eq "close") {
    Remove-Settings
    Write-Host "ポート転送とファイアウォール規則を削除しました。"
    Show-Status
    exit 0
}

# ---- open ----
$wsl = Get-WslAddress
Remove-Settings   # 古い設定（前回の WSL の IP など）を消してから入れ直す
& netsh.exe interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$Port connectaddress=$wsl connectport=$Port | Out-Null
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -Action Allow -Profile $NetworkProfile -RemoteAddress LocalSubnet `
    -Description "shogi-takeover の UI を同じ LAN の端末から開くための規則。takeover-lan.ps1 close で削除する。" | Out-Null

Write-Host "開きました（WSL の IP: $wsl、ポート: $Port、規則の対象: $NetworkProfile、同じ LAN 内の端末のみ）。"
$lan = @(Get-LanAddresses)
if ($lan.Count) {
    Write-Host "スマホで開く URL（token はアプリの起動時にコンソールへ表示されたもの）:"
    $lan | ForEach-Object { Write-Host "  http://$($_):$Port/?token=<トークン>" }
} else {
    Write-Host "この PC の LAN 側の IP アドレスは ipconfig で確認してください。"
}
Write-Host "使い終わったら close を実行してください。"
Write-Host ""
Show-Status
