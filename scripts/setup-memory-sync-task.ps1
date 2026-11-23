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

$vbs = Join-Path $PSScriptRoot "memory-sync-hidden.vbs"
if (-not (Test-Path $vbs)) { throw "找不到隐藏启动器：$vbs" }

# ★ 动作必须经 wscript + VBS 隐藏启动：Task Scheduler 直接起 powershell.exe 会弹控制台窗口，
#   -WindowStyle Hidden 也拦不住（实测每 10 分钟闪一次黑框）。
# ★ 用 schtasks.exe /XML 而不是 Register-ScheduledTask：
#   非管理员账户走 CIM 会 Access Denied (0x80070005)，同样的任务 schtasks 却能建成。
#   用 XML 是为了拿到两个 schtasks 命令行给不了的东西：
#     · LogonTrigger —— 登录后立刻同步一次，不用等下一个 10 分钟刻度
#     · StartWhenAvailable=true —— 关机/睡眠错过的时间点在恢复后补跑
$user = "$env:USERDOMAIN\$env:USERNAME"
$interval = "PT{0}M" -f $IntervalMinutes
$argsXml = "//nologo &quot;$vbs&quot;"   # XML 属性值里要转义引号

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$user</Author>
    <Description>AgentBrain memory vault sync (login + every $IntervalMinutes min, catch-up on resume)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>$interval</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>$argsXml</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path $env:TEMP ("agentbrain-sync-task-{0}.xml" -f [guid]::NewGuid().ToString("N"))
# schtasks /XML 要求 Unicode（UTF-16 LE + BOM）
[System.IO.File]::WriteAllText($xmlPath, $xml, (New-Object System.Text.UnicodeEncoding($false, $true)))

try {
    schtasks.exe /Create /TN $TaskName /XML $xmlPath /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks 创建失败（exit=$LASTEXITCODE）" }
} finally {
    Remove-Item -Force $xmlPath -ErrorAction SilentlyContinue
}

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host "已注册 $TaskName"
Write-Host ("  触发: " + (($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join " + "))
Write-Host ("  补跑: StartWhenAvailable=" + $t.Settings.StartWhenAvailable)
Write-Host "  手动跑一次：schtasks /Run /TN `"$TaskName`"（仅验收用，日常不用管）"
