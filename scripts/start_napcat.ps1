param([ValidatePattern('^$|^[0-9]{5,12}$')][string]$Account = '', [switch]$PlanOnly,
      [switch]$IfConfigured, [switch]$Background, [switch]$NoWatch)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$napcatRoot = Join-Path $taskRoot 'depends-data\napcat'
$launchConfig = Join-Path $napcatRoot 'config\maibot-launch.json'
function Start-LoginWatch {
    if ($NoWatch -or $PlanOnly) { return }
    $watchPath = Join-Path $PSScriptRoot 'napcat_login_watch.py'
    $pythonPath = (Get-Command 'python.exe' -ErrorAction Stop).Source
    Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', ('"' + $watchPath + '"'), '--watch') -WorkingDirectory $taskRoot -WindowStyle Hidden
    $lanConfig = Join-Path $napcatRoot 'config\lan-webui.json'
    if (Test-Path -LiteralPath $lanConfig) {
        $lanSettings = Get-Content -LiteralPath $lanConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($lanSettings.enabled) {
            $lanScript = Join-Path $PSScriptRoot 'napcat_lan_webui.py'
            Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', ('"' + $lanScript + '"')) -WorkingDirectory $taskRoot -WindowStyle Hidden
        }
    }
}
if ($IfConfigured) {
    if (-not (Test-Path -LiteralPath $launchConfig)) { exit 0 }
    $savedLaunch = Get-Content -LiteralPath $launchConfig -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $savedLaunch.autostart) { exit 0 }
}
if (-not $PlanOnly -and (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 6099 -State Listen -ErrorAction SilentlyContinue)) {
    Start-LoginWatch
    Write-Host 'NapCat WebUI is already running. Use open_napcat_webui.bat to log in.'
    exit 0
}
$launcherPath = Join-Path $napcatRoot 'NapCatWinBootMain.exe'
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw 'NapCat Shell is missing from depends-data\napcat.'
}
$qqPath = Join-Path $napcatRoot 'qq-client\Files\QQ.exe'
if (-not (Test-Path -LiteralPath $qqPath)) {
    throw 'Prepared QQ NT client is missing from depends-data\napcat\qq-client\Files\QQ.exe.'
}
$env:NAPCAT_PATCH_PACKAGE = Join-Path $napcatRoot 'qqnt.json'
$env:NAPCAT_LOAD_PATH = Join-Path $napcatRoot 'loadNapCat.js'
$env:NAPCAT_INJECT_PATH = Join-Path $napcatRoot 'NapCatWinBootHook.dll'
$env:NAPCAT_LAUNCHER_PATH = $launcherPath
$env:NAPCAT_MAIN_PATH = Join-Path $napcatRoot 'napcat.mjs'
if (-not $Account -and (Test-Path -LiteralPath $launchConfig)) {
    $Account = [string]((Get-Content -LiteralPath $launchConfig -Raw -Encoding UTF8 | ConvertFrom-Json).account)
    if ($Account -notmatch '^$|^[0-9]{5,12}$') { throw 'Invalid saved QQ quick-login account.' }
}
$loginArgs = @()
if ($Account) { $loginArgs = @('-q', $Account) }
if ($PlanOnly) {
    [pscustomobject]@{ launcher=$launcherPath; qq=$qqPath; quick_login=[bool]$Account; account=$Account; arguments=$loginArgs; background=[bool]$Background } | ConvertTo-Json -Compress
    exit 0
}
if ($Background) {
    $helperArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-NoWatch')
    if ($Account) { $helperArgs += @('-Account', $Account) }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $helperArgs -WorkingDirectory $napcatRoot -WindowStyle Hidden
    Start-LoginWatch
    exit 0
}
Write-Host 'Starting NapCat for interactive QQ login. MaiBot and WeChat processes are unchanged.'
Start-LoginWatch
Push-Location -LiteralPath $napcatRoot
try { & $launcherPath $qqPath $env:NAPCAT_INJECT_PATH @loginArgs } finally { Pop-Location }
