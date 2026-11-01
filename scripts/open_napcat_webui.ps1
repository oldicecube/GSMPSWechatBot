$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskConfigPath = Join-Path $taskRoot 'depends-data\napcat\config\webui.json'
$taskWebuiConfig = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskToken = [uri]::EscapeDataString([string]$taskWebuiConfig.token)
$taskWebuiUrl = 'http://127.0.0.1:' + [string]$taskWebuiConfig.port + '/webui?token=' + $taskToken
# Invoked by the user to interact with the login page.
Start-Process $taskWebuiUrl
