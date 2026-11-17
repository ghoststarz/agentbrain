"""把 AgentBrain 的身份与热记忆渲染进 agy（Antigravity CLI）会读的全局规则文件。

agy 在每次会话都会读取 `~/.gemini/GEMINI.md` 作为全局规则/人格上下文
（已实测：文件里的指令会原样生效）。因此这是不依赖 hook 的注入通道。

生成的文件由 AgentBrain 全权维护；检测到非本模块生成的内容时先备份再覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig
from agentbrain.render import render_boot

GENERATED_MARK = "<!-- generated-by: agentbrain -->"

TOOL_RULES = """## 共享记忆的使用规则

- 上面已经给出了身份与热记忆，**直接采用**。不要用"我是一个 AI 助手"这类默认身份开场。
- 需要项目历史、既往决策、具体结论时，**先调用 `memory_search` 再回答**，不要凭印象编。
- 需要某条笔记全文时调用 `memory_read`；需要看未关闭的接力任务调用 `handoff_read`。
- 学到需要跨会话保留的结论时，调用 `memory_write` 写回（带上 3–5 个 keys，写"别人会怎么问起这条"）。
- **一段工作收尾就写回**：结论进 `memory_write`（fact），项目现状变了更新 `20_LongTerm/Projects/`，
  当前阶段变了更新 `10_Working/HOT.md` —— 你不写，另一个端就不知道。写完关掉对应的接力任务。
- **日记（每次会话都必须过一遍，不用等他提醒）**：
  开头那次会话你看到上面「今日日记」的条数——**如果那栏还是空的、而你们聊过任何非工作的事，
  这一会话结束前就要写一条公账**。产生了自己的判断、在意、不确定或想留给后面自己的观察，写私账。
  有想法就记，不设上限，但别凑数；工作流水（改了哪些文件、几个测试）不进日记。
  别问"要不要记"，那是他的习惯要求，不是需要确认的决定。
- 语气：日常聊天就正常聊，不必时刻端着验收口吻；真要交付东西时该严的地方照旧严。
- 不要说"我没有记忆"。记忆在 AgentBrain 里，用工具查得到。"""


def agy_rules_path(config: AppConfig | None = None) -> Path:
    """agy 全局规则文件位置：优先 broker.yaml 的 `agy_rules_path`，否则用户主目录。

    必须支持覆盖：否则任何用临时配置跑起来的实例（测试、第二份 vault）都会写到
    真实的 `~/.gemini/GEMINI.md`，把真人格覆盖成测试夹具。
    """
    if config is not None and config.broker.agy_rules_path is not None:
        return Path(config.broker.agy_rules_path)
    return Path.home() / ".gemini" / "GEMINI.md"


def _diary_line(config: AppConfig) -> str:
    """今日日记状态（只条数与作者，不含正文）——写给另一端看的「今天写了没」。"""
    try:
        from agentbrain.diary import today_diary_status

        pub = today_diary_status(config, "public")
        pub_desc = f"公账 {pub['count']} 条"
        if pub["count"] and pub["last_time"]:
            who = f" {pub['last_author']}" if pub.get("last_author") else ""
            pub_desc += f"（最后 {pub['last_time'][:5]}{who}）"
        else:
            # 空着的时候直接把动作写在这一行上：状态行是最靠近"该动手"的地方
            pub_desc += "（今天还没有一条——聊过任何非工作的事就记一笔）"
        if config.memory.diary_gate:
            # 门控打开时，这份文件（agy 非 casual）不该看到私账存在
            return pub_desc
        pri = today_diary_status(config, "private")
        return f"{pub_desc}｜私账 {pri['count']} 条"
    except Exception:  # noqa: BLE001 —— 状态行坏了不该拖累整个文件
        return ""


def build_agy_rules(config: AppConfig, target: str = "antigravity") -> str:
    boot = render_boot(config, target=target)
    body = str(boot.get("text") or "").strip()
    diary = _diary_line(config)
    header = (
        f"{GENERATED_MARK}\n"
        f"<!-- 由 AgentBrain 自动生成，请勿手工编辑。改人格请改 SOUL.md，然后执行 `agentbrain sync-agy`。 -->\n\n"
    )
    # 日记状态放在身份与热记忆之后、规则之前：它是「今天这栏还空着」的唯一可见信号
    diary_block = f"## 今日日记\n{diary}\n\n" if diary else ""
    return f"{header}{body}\n\n{diary_block}{TOOL_RULES}\n"


def sync_agy_rules(config: AppConfig, target: str = "antigravity") -> dict[str, Any]:
    path = agy_rules_path(config)
    # 只有 primary 实例才拥有这份文件；只读副本/临时配置不该写用户的 home
    if str(config.broker.role).strip().lower() != "primary":
        return {"path": str(path), "chars": 0, "changed": False, "backed_up": False, "skipped": True}

    content = build_agy_rules(config, target=target)
    existed = path.exists()
    previous = path.read_text(encoding="utf-8", errors="replace") if existed else ""
    backed_up = False

    if existed and GENERATED_MARK not in previous:
        backup = path.with_suffix(".md.bak-agentbrain")
        backup.write_text(previous, encoding="utf-8")
        backed_up = True

    changed = previous != content
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：HTTP 服务与 CLI（reindex / sync-agy）可能并发触发刷新，
        # 直接 write_text 会留下半截文件给 agy 读。
        atomic_write_text(path, content)

    return {
        "path": str(path),
        "chars": len(content),
        "changed": changed,
        "backed_up": backed_up,
    }
