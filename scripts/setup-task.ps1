# 重建 AgentBrain 的计划任务：静默 + 自愈
# 1) 删掉会弹窗的 keepalive 轮询任务
# 2) 用 pythonw.exe（无控制台）承载服务，靠任务计划的重启策略自愈

$ErrorActionPreference = "Stop"

$task    = "AgentBrainBroker"
$oldTask = "AgentBrainBrokerKeepalive"
$repo    = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$exe     = Join-Path $repo ".venv\Scripts\pythonw.exe"
$wd      = $repo

# --- 清理旧任务 ---
foreach ($n in @($oldTask, $task)) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($t) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Output "removed: $n"
    }
}

# --- 先停掉当前占端口的实例（避免任务启动时冲突） ---
$listen = Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Stop-Process -Id $listen.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Write-Output "stopped existing broker pid=$($listen.OwningProcess)"
}

# --- 新任务：pythonw 无控制台，进程死掉由任务计划重启 ---
$action  = New-ScheduledTaskAction -Execute $exe -Argument "-m agentbrain serve --port 18765" -WorkingDirectory $wd
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Hidden

Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
    -Description "AgentBrain mem-broker HTTP service (windowless, auto-restart)" -Force | Out-Null

$t = Get-ScheduledTask -TaskName $task
Write-Output "--- $task ---"
Write-Output ("execute : " + $t.Actions[0].Execute)
Write-Output ("args    : " + $t.Actions[0].Arguments)
Write-Output ("hidden  : " + $t.Settings.Hidden)
Write-Output ("restart : count=" + $t.Settings.RestartCount + " interval=" + $t.Settings.RestartInterval)
Write-Output ("state   : " + $t.State)

# --- 立刻启动一次并验证 ---
Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 6
$up = Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
if ($up) {
    Write-Output "OK: listening on 18765 (pid=$($up.OwningProcess))"
} else {
    Write-Output "FAIL: nothing listening on 18765"
}
