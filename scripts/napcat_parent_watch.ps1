param(
    [Parameter(Mandatory = $true)][int]$ParentPid,
    [Parameter(Mandatory = $true)][string]$Root
)
# 独立看门狗：主 bot 进程退出（含崩溃/被强杀）后，关闭它拉起的 QQ 适配器。
$ErrorActionPreference = 'SilentlyContinue'
if ($ParentPid -le 0) { return }

while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 3
}

$ownerFile = Join-Path $Root 'depends-data\napcat\config\owner-pid.txt'
$current = ''
try { $current = (Get-Content -LiteralPath $ownerFile -Raw -ErrorAction Stop).Trim() } catch {}
if ($current -and $current -ne "$ParentPid") {
    $newPid = 0
    if ([int]::TryParse($current, [ref]$newPid) -and (Get-Process -Id $newPid -ErrorAction SilentlyContinue)) {
        return
    }
}

$python = (Get-Command 'python.exe' -ErrorAction Stop).Source
$code = "import sys; sys.path.insert(0, r'$Root'); from core.napcat_boot import stop_napcat; stop_napcat(r'$Root')"
& $python -X utf8 -c $code
