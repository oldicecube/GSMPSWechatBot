<#
.SYNOPSIS
    在独立窗口里显示 QQ 登录二维码，供手机 QQ 扫码。

.DESCRIPTION
    bot 启动时由 core/qq_adapter_boot.py 通过 cmd /c powershell -File 拉起，
    因此会出现在一个新的 cmd 窗口里。登录成功后 bot 会 taskkill 这个进程树，
    窗口自动关闭；二维码刷新时旧窗口也会被关掉再开一个新的。
#>
param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [string]$Uin = '',
    [int]$Scale = 3
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ImagePath)) {
    Write-Host "QR image not found: $ImagePath"
    Start-Sleep -Seconds 5
    exit 1
}

$img = [System.Drawing.Image]::FromFile((Resolve-Path -LiteralPath $ImagePath))
$w = [int]($img.Width * $Scale)
$h = [int]($img.Height * $Scale)

$form = New-Object System.Windows.Forms.Form
$form.Text = if ($Uin) { "QQ login - $Uin" } else { "QQ login" }
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.BackColor = [System.Drawing.Color]::White
$form.ClientSize = New-Object System.Drawing.Size(($w + 40), ($h + 96))

$pic = New-Object System.Windows.Forms.PictureBox
$pic.Location = New-Object System.Drawing.Point(20, 20)
$pic.Size = New-Object System.Drawing.Size($w, $h)
$pic.SizeMode = 'StretchImage'
$pic.Image = $img
$form.Controls.Add($pic)

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(20, ($h + 28))
$label.Size = New-Object System.Drawing.Size(($w + 40), 56)
$label.TextAlign = 'MiddleCenter'
$label.Text = "Scan with mobile QQ" + [Environment]::NewLine + "This window closes after login"
if ($Uin) { $label.Text = "Scan with mobile QQ ($Uin)" + [Environment]::NewLine + "This window closes after login" }
$form.Controls.Add($label)

[void]$form.ShowDialog()
$img.Dispose()
