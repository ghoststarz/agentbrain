# obsidian-broker · Hermes MemoryProvider 插件

把 [AgentBrain mem-broker](../../../README.md) 接到 Hermes Agent 的**每轮记忆注入**通道上。

Hermes 的 `MemoryProvider` 接口允许插件在三个位置介入：系统提示词（静态）、每轮用户输入前（prefetch）、以及会话事件（写入 / 压缩前）。这个插件是那份实现，单文件、无第三方依赖，只走本机 HTTP。

## 它做了什么

| 钩子 | 行为 | 预算 |
|---|---|---|
| `system_prompt_block` | 注入一小段静态协议说明（记忆由 broker 托管、要查事实先检索） | 450 字硬顶 |
| `prefetch` | 每次用户输入 → `POST /v1/search`（k=5, hops=1）→ 命中注入成 `[MEMORY_PREFETCH]` 块；revision 变了额外带一行 `[MEMORY_DELTA]` | 1200 字硬顶 |
| `sync_turn` | 可选：把每轮 U/A 摘要追加进公账日记（`memory.yaml` 的 `diary.turn_capture` 控制，默认关） | 各 200 字 |
| `on_memory_write` | Hermes 侧 `memory` 工具写入时，镜像成一条 `fact` 笔记进 vault | 无 |
| `on_pre_compress` | 上下文压缩前，把当前任务 upsert 成 `handoff` 里的 `in_progress` 槽位，压缩后不丢线 | 240 字 |
| `get_tool_schemas` | **返回空**——插件不向会话暴露任何额外工具面 | — |

设计底线是 **fail-open**：每个钩子整段包 `try/except`，HTTP 超时 0.9–1.5 秒，broker 没起、token 错、返回坏 JSON，一律静默降级成"这轮没有记忆",对话照常。

## 安装

Hermes 会扫描 `<hermes_home>/plugins/<name>/plugin.yaml` 并调用同目录 `__init__.py` 的 `register(ctx)`。

```bash
# 方式一：目录联接 / 软链（推荐——仓库是唯一真源，改一处就生效）
# Windows（PowerShell，无需管理员）：
powershell -NoProfile -Command "New-Item -ItemType Junction -Path \"$env:LOCALAPPDATA\hermes\profiles\<profile>\plugins\obsidian-broker\" -Target \"<repo>\integrations\hermes\obsidian-broker\""
# POSIX：
ln -s <repo>/integrations/hermes/obsidian-broker ~/.hermes/profiles/<profile>/plugins/obsidian-broker

# 方式二：复制（简单，但之后两边会漂移）
cp -r integrations/hermes/obsidian-broker "%LOCALAPPDATA%\hermes\profiles\<profile>\plugins\"
```

先建链接、确认能读到目标目录里的 `plugin.yaml`，再删旧副本。注意在 MSYS/Git-Bash 里 `cmd //c mklink /J` 可能被路径转换吃掉、静默不执行。

装完重启 Hermes（新会话生效）。

## 配置

1. **端口**：读 `broker.yaml` 的 `port`（默认 18765），路径同 broker 本体（`AGENTBRAIN_BROKER_CONFIG` 可覆盖）。
2. **token**：按优先级取 `AGENTBRAIN_TOKEN` 环境变量 → `broker.yaml` 的 `token` → `<hermes_home>/.env` 里 `AGENTBRAIN_TOKEN=` 那一行。这是 `/v1/*` 的 `Authorization: Bearer` 凭据，缺了只能拿到 `/healthz`。
3. **vault 归属**：`sync_turn` 的开关从 `<vault_root>/profiles/<profile>/memory.yaml` 的 `diary.turn_capture` 读。

## 排查

日志写在 `%LOCALAPPDATA%/agentbrain/logs/provider.log`，logger 名 `agentbrain.obsidian_broker`。常见症状：

- **完全没有注入**：先 `curl http://127.0.0.1:18765/healthz`；服务没起就 `python -m agentbrain serve`。
- **只有 `[MEMORY_PREFETCH]` 没有内容**：检索命中全是 `weak_evidence`，插件会整段丢弃（证据不够就不占预算）。
- **写入没到 vault**：检查 token 是否 401 —— `provider.log` 里会有 `prefetch failed: HTTP Error 401`。

## 与 broker 版本的关系

插件只依赖 HTTP 契约（`/v1/search`、`/v1/diary`、`/v1/handoff`、`/v1/write`），不 import broker 的 Python 包，所以两者可以各自升级。契约改了就要一起改——这是唯一的耦合点。
