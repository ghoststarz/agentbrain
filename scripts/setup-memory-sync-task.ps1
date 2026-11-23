<#
注册 / 重建 AgentBrain 记忆同步计划任务。

  AgentBrainMemorySync  每 10 分钟 + 登录时，把 vault 变更 commit & push

用法（普通权限即可）：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup-memory-sync-task.ps1
#>
param(
    [string]$TaskName = "AgentBrainMemorySync",
    [int]$IntervalMinutes = 10,
    [string]$ScriptPath = (Join-Path $PSScriptRoot "memory-sync.ps1")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ScriptPath)) { throw "找不到同步脚本：$ScriptPath" }

$tr = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""

# ★ 用 schtasks.exe 而不是 Register-ScheduledTask：
#   本机账户不是管理员，cmdlet 走 CIM 会 Access Denied (0x80070005)，同样的任务 schtasks 却能建成。
schtasks.exe /Create /TN $TaskName /SC MINUTE /MO $IntervalMinutes /TR $tr /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks 创建失败（exit=$LASTEXITCODE）" }

Write-Host "已注册 $TaskName（登录后每 $IntervalMinutes 分钟；错过的时间点由下一次触发补上）"
schtasks.exe /Query /TN $TaskName /FO LIST | Select-String "任务名|TaskName|下次运行|Next Run|状态|Status"
