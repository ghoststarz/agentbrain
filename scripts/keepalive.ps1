# AgentBrain broker keepalive: start the HTTP service if port 18765 is not listening.
# Registered as a scheduled task repeating every 5 minutes.

$ErrorActionPreference = "SilentlyContinue"

$port = 18765
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$exe  = Join-Path $repo ".venv\Scripts\python.exe"
$wd   = $repo
$log  = "$env:LOCALAPPDATA\agentbrain\logs\keepalive.log"

$listening = Get-NetTCPConnection -LocalPort $port -State Listen
if ($listening) { exit 0 }

New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
"$(Get-Date -Format s)  port $port not listening, starting broker" | Out-File -FilePath $log -Append -Encoding utf8

Start-Process -FilePath $exe -ArgumentList "-m","agentbrain","serve","--port",$port -WorkingDirectory $wd -WindowStyle Hidden

Start-Sleep -Seconds 5
$now = Get-NetTCPConnection -LocalPort $port -State Listen
if ($now) {
    "$(Get-Date -Format s)  started ok" | Out-File -FilePath $log -Append -Encoding utf8
} else {
    "$(Get-Date -Format s)  FAILED to start" | Out-File -FilePath $log -Append -Encoding utf8
}
