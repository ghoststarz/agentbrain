<#
AgentBrain 记忆库跨机同步（阶段 1′）

顺序固定：
  1. 确保 .gitattributes（* text=auto 与 **/30_Diary/** merge=union）
  2. commit 本地变更
  3. git fetch
  4. git pull --rebase
     - 若有冲突：调 python -m agentbrain resolve-conflicts -> git add -A && git rebase --continue
     - 若 unresolved 非空或 rebase 无法继续：git rebase --abort，放弃本次 push，写 SEVERE 日志并落一条 kind=severe 的 notice
  5. python -m agentbrain reindex
  6. git push

退出码恒为 0（计划任务里不该因为网络抖动报警）。
#>
param(
    [string]$VaultRoot = "D:\Files\warehouse\Starlis\Asteria Lily\Memory",
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$PythonExe = "E:\Code\mem-broker\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Continue"

# ★ 外部程序（python）的 UTF-8 输出必须按 UTF-8 解码：默认控制台代码页会把中文 JSON 解成乱码，
#   ConvertFrom-Json 随即失败，整次同步被判 SEVERE 中止。下面前两句给控制台解码兜底，
#   真正的契约是 resolve-conflicts 的 --out 文件（见下方），不依赖控制台编码。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { $OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$stateDir = Join-Path $env:LOCALAPPDATA "agentbrain\state"
$logDir = Join-Path $env:LOCALAPPDATA "agentbrain\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$logFile = Join-Path $logDir "memory-sync.log"
$noticesFile = Join-Path $stateDir "notices.jsonl"

function Resolve-StateDir() {
    # notices 要写进 broker 实际使用的 state_dir：broker.yaml 可改它，写死 LOCALAPPDATA 会两边读不到。
    $cfg = $env:AGENTBRAIN_BROKER_CONFIG
    if (-not $cfg -or -not (Test-Path $cfg)) { $cfg = Join-Path $env:LOCALAPPDATA "agentbrain\broker.yaml" }
    if (Test-Path $cfg) {
        foreach ($line in (Get-Content -Path $cfg -Encoding utf8 -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*state_dir:\s*"?([^"\r\n]+?)"?\s*$') {
                return ($matches[1].Trim() -replace '/', '\')
            }
        }
    }
    return (Join-Path $env:LOCALAPPDATA "agentbrain\state")
}

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

function Write-SevereNotice([string]$text) {
    try {
        $clean = ($text -replace "\s+", " ").Trim()
        if ($clean.Length -gt 120) {
            $clean = $clean.Substring(0, 119).TrimEnd() + "…"
        }
        $entry = [ordered]@{
            id           = ([guid]::NewGuid().ToString("N").Substring(0, 12))
            ts           = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss+00:00")
            kind         = "severe"
            text         = $clean
            delivered_at = $null
        }
        $json = $entry | ConvertTo-Json -Compress
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $target = Join-Path (Resolve-StateDir) "notices.jsonl"
        [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
        [System.IO.File]::AppendAllText($target, $json + "`n", $utf8NoBom)
    } catch {
        Write-Log "WARN 写入 severe notice 失败：$($_.Exception.Message)"
    }
}

function Ensure-GitAttributes([string]$root) {
    $attrPath = Join-Path $root ".gitattributes"
    $required = @("* text=auto", "**/30_Diary/** merge=union")
    $existing = @()
    if (Test-Path $attrPath) {
        $existing = @(Get-Content -Path $attrPath -Encoding utf8 -ErrorAction SilentlyContinue)
    }
    # 旧版本写错过模式：`*/30_Diary/**` 只匹配一层目录，匹配不到 profiles/<id>/30_Diary/，
    # 等于日记的 union 合并从来没生效过。就地迁移成 `**/30_Diary/**`。
    $legacy = @("*/30_Diary/** merge=union")
    $legacyHit = $false
    $filtered = @()
    foreach ($line in $existing) {
        if ($legacy -contains $line.Trim()) {
            $legacyHit = $true
            continue
        }
        $filtered += $line
    }
    $existing = $filtered
    $missing = @()
    foreach ($rule in $required) {
        if ($existing -notcontains $rule) {
            $missing += $rule
        }
    }
    if ($missing.Count -gt 0 -or $legacyHit) {
        $allLines = @()
        if ($existing) { $allLines += $existing }
        $allLines += $missing
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($attrPath, ($allLines -join "`n") + "`n", $utf8NoBom)
        Write-Log "ATTR 已补齐 .gitattributes（+$($missing.Count) 条规则，legacy=$legacyHit）"
    }
}

function Test-RebaseInProgress([string]$root) {
    return (Test-Path (Join-Path $root ".git\rebase-merge")) -or (Test-Path (Join-Path $root ".git\rebase-apply"))
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
    Ensure-GitAttributes $VaultRoot

    # 1. commit 本地变更
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

    # 2. git fetch
    $fetchOut = git fetch $Remote $Branch 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARN fetch 失败（离线或远端不可达，跳过本次 pull/push）：$($fetchOut -join ' ')"
        exit 0
    }

    # 3. git pull --rebase
    $pullOut = git pull --rebase $Remote $Branch 2>&1
    $pullExit = $LASTEXITCODE
    if ($pullExit -ne 0) {
        Write-Log "WARN pull --rebase 遇到冲突或异常，尝试自动裁决"
        $abortSync = $false
        $abortReason = ""
        $round = 0
        $maxRounds = 10

        while ((Test-RebaseInProgress $VaultRoot) -and ($round -lt $maxRounds)) {
            $round += 1
            $unmerged = @(git diff --name-only --diff-filter=U 2>$null | Where-Object { $_.Trim() -ne "" })
            if ($unmerged.Count -eq 0) {
                $abortSync = $true
                $abortReason = "rebase 中断但未检测到未合并文件：$($pullOut -join ' ')"
                break
            }

            if (-not (Test-Path $PythonExe)) {
                $abortSync = $true
                $abortReason = "找不到解释器 $PythonExe，无法执行 resolve-conflicts"
                break
            }

            $rcOutFile = Join-Path $env:TEMP ("agentbrain-resolve-{0}.json" -f ([guid]::NewGuid().ToString("N")))
            $rcNotices = Join-Path (Resolve-StateDir) "notices.jsonl"
            $rcArgs = @("-m", "agentbrain", "resolve-conflicts", "--vault", $VaultRoot, "--files") + $unmerged +
                @("--out", $rcOutFile, "--notices-path", $rcNotices)
            $rcRaw = & $PythonExe @rcArgs 2>&1
            $rcExit = $LASTEXITCODE
            $rcText = ""
            if (Test-Path $rcOutFile) {
                $rcText = (Get-Content -Path $rcOutFile -Raw -Encoding utf8).Trim()
                Remove-Item -Force $rcOutFile -ErrorAction SilentlyContinue
            }
            if (-not $rcText) {
                # 兜底：旧版没有 --out 时才依赖 stdout（可能受控制台编码影响）
                $rcText = ($rcRaw | Out-String).Trim()
            }
            if ($rcExit -ne 0) {
                $abortSync = $true
                $abortReason = "resolve-conflicts 失败（exit=$rcExit）：$rcText"
                break
            }

            try {
                $rcObj = $rcText | ConvertFrom-Json
            } catch {
                $abortSync = $true
                $abortReason = "resolve-conflicts 输出非合法 JSON：$rcText"
                break
            }

            $unresolvedCount = 0
            if ($rcObj.unresolved) {
                $unresolvedCount = @($rcObj.unresolved).Count
            }
            if ($unresolvedCount -gt 0) {
                $abortSync = $true
                $abortReason = "存在 $unresolvedCount 个无法自动裁决的冲突文件，已归档本地副本并放弃本次推送"
                break
            }

            git add -A 2>&1 | Out-Null
            $contOut = git -c core.editor=true rebase --continue 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Log "CONFLICT 第 $round 轮自动裁决完成并继续 rebase"
                break
            }
            $pullOut = $contOut
        }

        if ((Test-RebaseInProgress $VaultRoot) -and (-not $abortSync)) {
            $abortSync = $true
            $abortReason = "rebase 超过 $maxRounds 轮仍未完成，已中止"
        }

        if ($abortSync) {
            git rebase --abort 2>&1 | Out-Null
            Write-Log "SEVERE 同步冲突中止 rebase 并放弃本次 push：$abortReason"
            Write-SevereNotice "跨机同步冲突中止：$abortReason"
            exit 0
        }
    }

    # 4. pull 之后执行 reindex
    if (Test-Path $PythonExe) {
        $reindexOut = & $PythonExe -m agentbrain reindex 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Log "REINDEX ok"
        } else {
            Write-Log "WARN reindex 失败：$($reindexOut -join ' ')"
        }
    } else {
        Write-Log "WARN 找不到解释器 $PythonExe，跳过 reindex"
    }

    # 5. push
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
