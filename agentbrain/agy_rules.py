"""把 AgentBrain 的身份与热记忆渲染进 agy（Antigravity CLI）会读的全局规则文件。

agy 在每次会话都会读取 `~/.gemini/GEMINI.md` 作为全局规则/人格上下文
（已实测：文件里的指令会原样生效）。因此这是不依赖 hook 的注入通道。

生成的文件由 AgentBrain 全权维护；检测到非本模块生成的内容时先备份再覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentbrain.config import AppConfig
from agentbrain.render import render_boot

GENERATED_MARK = "<!-- generated-by: agentbrain -->"

TOOL_RULES = """## 共享记忆的使用规则

- 上面已经给出了身份与热记忆，**直接采用**。不要用"我是一个 AI 助手"这类默认身份开场。
- 需要项目历史、既往决策、具体结论时，**先调用 `memory_search` 再回答**，不要凭印象编。
- 需要某条笔记全文时调用 `memory_read`；需要看未关闭的接力任务调用 `handoff_read`。
- 学到需要跨会话保留的结论时，调用 `memory_write` 写回（带上 3–5 个 keys，写"别人会怎么问起这条"）。
- 不要说"我没有记忆"。记忆在 AgentBrain 里，用工具查得到。"""


def agy_rules_path() -> Path:
    return Path.home() / ".gemini" / "GEMINI.md"


def build_agy_rules(config: AppConfig, target: str = "antigravity") -> str:
    boot = render_boot(config, target=target)
    body = str(boot.get("text") or "").strip()
    header = (
        f"{GENERATED_MARK}\n"
        f"<!-- 由 AgentBrain 自动生成，请勿手工编辑。改人格请改 SOUL.md，然后执行 `agentbrain sync-agy`。 -->\n\n"
    )
    return f"{header}{body}\n\n{TOOL_RULES}\n"


def sync_agy_rules(config: AppConfig, target: str = "antigravity") -> dict[str, Any]:
    path = agy_rules_path()
    content = build_agy_rules(config, target=target)
    existed = path.exists()
    previous = path.read_text(encoding="utf-8", errors="replace") if existed else ""
    backed_up = False

    if existed and GENERATED_MARK not in previous:
        backup = path.with_suffix(".md.bak-agentbrain")
        backup.write_text(previous, encoding="utf-8")
        backed_up = True

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    return {
        "path": str(path),
        "chars": len(content),
        "changed": previous != content,
        "backed_up": backed_up,
    }
