# AgentBrain 主动记忆整理（Stage 2）启用与交接指南

> **状态说明**：本功能默认**关闭**（`proactive.enabled: false`，`proactive.event_capture: false`）。仓库已完成离线隔离测试，**未**修改你的真实 Vault、`memory.yaml`、已安装的 Hermes 插件或重启后台服务。

---

## 0.1 什么算“一个用户轮次”

只有**真人发言**计数。以下以 `[IMPORTANT:`、`[INTERNAL NOTIFICATION`、`[PREVIOUS_REVIEW_RESULT]`、`[DIARY]` 开头或含 `heartbeat #` 的消息会被判定为系统通知：仍然入库、仍然作为复盘证据，但 `countable=0`，不推进 5 轮计数，且证据里最多保留最近 2 条、每条截断 400 字。`[OUT-OF-BAND USER MESSAGE]` 是真人插话，正常计数。

- 相邻≤120 秒、正文（剥掉进程/任务 ID 后）完全相同的**系统通知**会被折叠，避免 agy 结果回灌刷屏；真人重复发言（例如连发“继续”）各自成轮。
- 历史库口径升级后可执行 `python -m agentbrain review-reconcile-countable`（幂等，带 `--force` 可重跑）重算 `countable` 与轮次水位；水位按「旧编号已跑批次覆盖到哪里」换算，不会把已复盘内容重排。
- 迁移前备份：`%LOCALAPPDATA%\agentbrain\backups\countable-<stamp>\lily.db`。

## 0. 提示词放在哪（可手改，不必重启）

| 文件 | 作用 |
| --- | --- |
| `agentbrain/prompt_assets/review_system.md` | 复盘主提示词：5 轮增量与 22:30 夜间**共用这一份**（人格部分运行时从 SOUL.md 热读） |
| `agentbrain/prompt_assets/review_nightly_addendum.md` | 夜间整轮要求：必须各写一条公账与私账 |
| `agentbrain/prompt_assets/review_nightly_segment.md` | 夜间多分段时的分段要求：只输出 `digest`，不写日记 |
| `agentbrain/prompt_assets/review_repair.md` | 批量校验失败后的一次「只修格式」重试指令 |
| `agentbrain/prompt_assets/review_retry_compact.md` | 模型输出被截断时的重试指令 |
| `agentbrain/prompt_assets/agy_tool_rules.md` | 写进 `~/.gemini/GEMINI.md` 的 agy 记忆规则（改后跑 `agentbrain sync-agy`） |

加载器 `agentbrain/prompt_lib.py` 按 `mtime + size` 热读取：改 md 后下一次复盘立即生效，无需重启 broker；
文件缺失或为空会抛 `PromptAssetError`，不会静默退化成空提示词。
`~/.gemini/GEMINI.md` 是生成物，请勿手改——改上面的 md，再 `agentbrain sync-agy`。

## 1. 离线已测 vs 真实环境未测边界

### 已在离线隔离环境验证
- Stage 1 调度核心强化：超长事件正文无损分块（`event_chunks`）与超硬上限显式 `PayloadTooLargeError` / HTTP `413` 拒绝（不再静默截断）；夜间补录跨不同 `window_id` 严格按时间先后顺序执行。
- 配置 fail-closed：畸形 `enabled`、越界 `nightly_hour/minute`、在 `model_api_key_env` 中直接填入 `sk-...` 等密钥均在加载/启动阶段直接拒绝。
- Hermes 插件源码（`integrations/hermes/agentbrain/__init__.py`）：纯标准库解析配置（无 PyYAML 依赖）、本地持久 outbox 重试、相同短句连续多轮独立计数、系统/bot/评审后台轮次过滤、`<think>` 与注入记忆块剥离、`on_delegation` 仅作 `reference_only` 采集。
- agy IDE 适配器：真实 `transcript.jsonl` 格式解析、工具链归并、部分行忽略、文件重写/截断保留未完成轮次、首次激活基线尾随不追溯历史、缺失 JSONL 可见标记 `source_unavailable_no_jsonl`（不解析 protobuf）。
- 假本地 OpenAI 兼容 HTTP 服务器：结构化 JSON 响应校验、`no_change` 零写入、凭证脱敏、上下文预算确定性分段及单条超限显式失败。
- 受限输出投影与安全边界：整批先校验后写入、禁止写入 `SOUL.md`/`USER.md`/`RULES.md`/配置文件与目录穿越/符号链接、未验证结论降级为 `report`、项目页受管块保留手工文本并由受管块生成 `_Overview.md`、`expected_hash` 冲突检测、崩溃重放日记不重复、日记按事件/窗口日期写入且不入索引。
- 进程级耐久租约互斥、HTTP `/v1/review/*` 接口与 CLI 诊断子命令。

### 尚未在真实环境验证（需由你亲自启用并验收）
- 真实 Hermes 运行时 `background_review` / 主模型路由调用（或可选 OpenAI 兼容端点）与实际提取质量。
- 真实 `agy` IDE 运行目录（`brain/*/.system_generated/logs/transcript.jsonl`）在实时对话中的增量捕获。
- 真实 Hermes 运行时加载更新后的 `agentbrain` 插件与 `/new` 会话流转。
- 真实 OS 本地时间 22:30 跨夜及漏夜追平运行。

---

## 2. 启用前备份

在修改任何真实配置前，建议先备份：
1. Vault 中的 `profiles/<profile>/memory.yaml`（如 `profiles/lily/memory.yaml`）
2. 运行时目录 `%LOCALAPPDATA%\agentbrain\`（POSIX 为 `~/.local/share/agentbrain/`，至少备份 `state/` 与 `broker.yaml`）
3. 当前已安装的 Hermes `agentbrain` 插件文件（若为目录联接或复制文件，先保留原副本）

---

## 3. 配置示例（`profiles/<profile>/memory.yaml`）

在 `memory.yaml` 中加入或更新 `proactive:` 段。**默认走 Hermes 框架路由**（复用 Hermes 的 `auxiliary.background_review` 或主 profile 模型与框架鉴权），**无需**单独填写 `model_endpoint`、`model_name` 或 `model_api_key_env`；`diary.turn_capture` 保持 `false`：

```yaml
diary:
  gate: false
  turn_capture: false

proactive:
  enabled: true
  event_capture: true
  incremental_batch_size: 5
  nightly_hour: 22
  nightly_minute: 30
  archive_enabled: true        # 夜间复盘末尾执行 14 天遗忘归档
  archive_after_days: 14
  lease_seconds: 300
  max_attempts: 5
  base_backoff_seconds: 30
  max_backoff_seconds: 3600
  max_payload_chars: 16000
  max_payload_chunks: 16
  retention_days: 30

  # agy IDE 适配器（显式指定 brain 根目录列表；支持环境变量与 ~；不开启则仅接收 HTTP/CLI 上报）
  agy_ide_enabled: true
  agy_ide_roots:
    - "%USERPROFILE%/.gemini/antigravity/brain"
  agy_ide_poll_seconds: 30

  # 后台评审模型：默认复用 Hermes 框架路由（auxiliary.background_review -> 主模型回退），无需单独配置端点与密钥
  model_backend: "hermes"
  hermes_profile: "lily"
  # 可选可移植路径覆盖（留空时自动从平台默认 Hermes 目录与 installs/*/facts.json 发现）：
  # hermes_home: "%LOCALAPPDATA%/hermes"
  # hermes_agent_dir: "%LOCALAPPDATA%/hermes/hermes-agent"
  worker_poll_seconds: 15
  model_timeout_seconds: 30.0
  model_max_context_chars: 24000
  model_max_output_tokens: 2000
```

> **可选向后兼容（OpenAI 兼容端点）**：若不走 Hermes 框架路由，也可显式设置 `model_backend: "openai_compatible"` 并提供 `model_endpoint`、`model_name` 及环境变量名 `model_api_key_env`（仅写环境变量名，绝不写明文密钥）。

---

## 4. Hermes 插件源码更新与启用

本阶段**未改动**你已安装的 Hermes 插件。若你的 Hermes 插件目录不是指向 `<repo_root>/integrations/hermes/agentbrain/` 的目录联接，请手动将最新源码复制过去，并**重启 Hermes 进程**以确保真正重载新的 Python 插件模块（仅执行 `/new` 可能不会重载已导入的插件代码）：

```powershell
# 如需手动复制（请先确认目标路径并备份旧文件）
Copy-Item "<repo_root>\integrations\hermesgentbrain\__init__.py" "<你的_hermes_plugin_dir>\__init__.py"
```

---

## 5. 诊断、试运行与控制命令

在仓库根目录下使用虚拟环境执行（`review-status` 等状态命令只输出脱敏元信息，不含聊天原文；**注意 `--export-prompts` 导出的提示词文件包含 `<existing_memory>` 与 `<untrusted_evidence>` 敏感证据原文，使用后请及时删除**）：

```powershell
# 1. 查看主动评审队列、窗口、会话计数与适配器游标状态
.\.venv\Scripts\python.exe -m agentbrain review-status

# 2. 手动执行一次夜间窗口检查与可选 IDE 扫描
.\.venv\Scripts\python.exe -m agentbrain review-tick

# 3. 导出待评审任务的提示词到临时目录（不调用模型、不写 Vault；含敏感证据原文，用后请删除）
.\.venv\Scripts\python.exe -m agentbrain review-run --export-prompts ".\scratch_prompts"

# 4. 试运行一次 worker（调用配置的模型并校验输出，但不写入 Vault）
.\.venv\Scripts\python.exe -m agentbrain review-run --dry-run

# 5. 手动记录一条 agy CLI 参考事件（仅作证据，不计入人类完成轮次）
.\.venv\Scripts\python.exe -m agentbrain review-cli-event --session-id "cli-demo" --content "完成某个脚本验证"

# 6. 显式从头补录 agy_ide_roots 历史（仅在需要追溯历史时使用）
.\.venv\Scripts\python.exe -m agentbrain review-catchup
```

HTTP 服务运行中也可通过鉴权请求查看状态：`GET http://127.0.0.1:18765/v1/review/status`。

---

## 6. 禁用与回滚方法

- **仅暂停后台评审与采集**：在 `memory.yaml` 中将 `proactive.enabled: false` 与 `proactive.event_capture: false`，重启 `AgentBrainBroker` 服务即可（已存在的 Vault 笔记不受影响）。
- **彻底回滚**：恢复备份的 `memory.yaml` 与旧版插件文件，并在 Hermes 执行 `/new`；如需清理调度状态库，删除 `state_dir/proactive/<profile>.db` 即可（不会影响 Vault 笔记）。

---

## 7. 基线启用流程与真实环境验收清单

### 0. 基线启用流程（不导入历史会话）
1. **备份与配置**：完成第 2 节备份后，按第 3 节在 `memory.yaml` 中启用 `proactive`（默认 `model_backend: "hermes"`，`hermes_profile: "lily"`，无需额外模型/密钥配置）。
2. **建立 IDE 基线（先于采集）**：在开启新对话前，先执行一次 `.\.venv\Scripts\python.exe -m agentbrain review-tick`（或重启 `AgentBrainBroker` 服务）。首次扫描会对已有 `agy_ide_roots` 下的 `transcript.jsonl` 从 **EOF** 记录基线游标，**不会**回溯导入历史对话（只有显式执行 `review-catchup` 才会导入历史）。
3. **Hermes 新会话**：重启 Hermes 进程（确保加载最新 `agentbrain` 插件），并执行 `/new` 开启新会话，既有历史会话不会被采集。

### A. 5 轮增量触发验收（Hermes 与 agy IDE）
1. 确认 `proactive.enabled: true`、`proactive.event_capture: true`、`proactive.agy_ide_enabled: true`（如测 IDE）且已重启 `AgentBrainBroker` 与 Hermes 进程。
2. **Hermes 端**：在 Hermes 中开启新会话，连续完成 5 轮有效对话（可包含连续相同的短句如「继续」）。
3. **agy IDE 端**：在 `agy` IDE 中新建一个对话，连续完成 5 轮有效对话，等待超过 `agy_ide_poll_seconds`（默认 30s）。
4. 运行 `review-status`：确认 Hermes 与 `agy_ide` 会话均达到 `completed_human_turns == 5`，各自生成 `incremental` 任务并成功变为 `succeeded`（`outcome` 为 `applied` 或 `no_change`）。
5. 检查对应笔记或 `30_Diary/` 是否符合预期、未触碰 `SOUL.md`、手工项目段落完好。

### B. 22:30 夜间窗口验收
1. 在当日有过对话后，等待 OS 本地时间越过 22:30（或重启后首个周期 tick）。
2. 运行 `review-status`：确认 `nightly_windows` 生成当日窗口并执行 `nightly` 任务；若当日无有效事件则记录为 `skipped_empty` 且不调用模型。
3. 若产出了日记，确认文件名日期为窗口日期而非次日日期。

### C. 漏夜追平验收
1. 关闭服务跨过 1 个或多个 22:30（期间如有离线 IDE 记录或无记录）。
2. 重新启动服务后观察 `review-status`：确认空窗口被跳过，非空窗口按 `window_id` 时间先后顺序逐一完成追平，无重复写入与重复日记。
