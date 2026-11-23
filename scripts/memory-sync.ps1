<#
AgentBrain 记忆库跨机同步（阶段 0）

把 vault_root 的 markdown 变更提交并推送到私有仓库。
设计口径：
  - 只做「提交 + 推送」，不 pull、不 rebase —— 写者唯一，避免自动合并改坏笔记
  - 离线/推送失败不算错误：先本地 commit，下一次运行自然把堆积的提交推上去
  - 日志落在 broker 的 logs 目录，和 provider 日志放一起

退出码恒为 0（计划任务里不该因为网络抖动报警）。
#>
param(
    [string]$VaultRoot = "D:\Files\warehouse\Starlis\Asteria Lily\Memory",
    [string]$Remote = "origin",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Continue"

$logDir = Join-Path $env:LOCALAPPDATA "agentbrain\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "memory-sync.log"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

# 日志轮转：超过 1MB 就滚一份
try {
    if ((Get-Item $logFile -ErrorAction SilentlyContinue).Length -gt 1MB) {
        Move-Item -Force $logFile "$logFile.1"
    }
} catch { }

if (-not (Test-Path (Join-Path $VaultRoot ".git"))) {
    Write-Log "SKIP vault 不是 git 仓库：$VaultRoot"
    exit 0
}

Push-Location $VaultRoot
try {
    $status = git status --porcelain --untracked-files=all 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR git status 失败：$status"
        exit 0
    }

    if ($status) {
        git add -A 2>&1 | Out-Null
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $changed = (git diff --cached --name-only 2>$null | Measure-Object).Count
        git commit -q -m "memory: $stamp ($changed files)" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Log "COMMIT $changed files"
        } else {
            Write-Log "WARN commit 无变更或失败"
        }
    }

    # 有未推送提交就尝试推；失败只记日志
    $ahead = git rev-list --count "$Remote/$Branch..HEAD" 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$ahead -gt 0) {
        $out = git push $Remote "HEAD:$Branch" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Log "PUSH $ahead commits"
        } else {
            Write-Log "WARN push 失败（下次重试）：$($out -join ' ')"
        }
    }
} catch {
    Write-Log "ERROR $($_.Exception.Message)"
} finally {
    Pop-Location
}

exit 0
