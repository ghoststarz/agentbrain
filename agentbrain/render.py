"""按端预算渲染启动块（boot）、增量块（delta）与预取摘要（prefetch）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agentbrain.config import AppConfig
from agentbrain.handoff import list_open_handoffs
from agentbrain.index import connect_db

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)


def get_current_revision(config: AppConfig) -> int:
    """从索引库读取当前 profile 的 revision；库不存在时返回 0。"""
    db_path = config.broker.db_path
    if not db_path.exists():
        return 0
    with connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT revision FROM rev WHERE scope = ?",
            (config.memory.profile,),
        ).fetchone()
        return int(row["revision"]) if row else 0


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1].rstrip() + "…"


def _extract_section(markdown: str, heading_pattern: str) -> str:
    """提取指定标题到下一个同级或更高级标题之间的内容。"""
    lines = markdown.splitlines()
    captured: list[str] = []
    in_section = False
    base_level = 0
    pat = re.compile(heading_pattern)

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            if not in_section and pat.search(line):
                in_section = True
                base_level = level
                captured.append(line)
                continue
            if in_section and level <= base_level:
                break
        if in_section:
            if line.strip() == "---":
                break
            captured.append(line)
    return "\n".join(captured).strip()


_TARGET_FRAMES = {
    "antigravity": "Antigravity CLI",
    "codex": "Codex CLI",
    "hermes": "Hermes Agent",
}


def _identity_intro(markdown: str, target: str) -> str:
    """取 SOUL 开头的自我陈述段落（`# SOUL` 之后、第一个 `##` 之前），换掉框架名。

    这段话是人格的「我是谁」，比特质列表重要得多：没有它，另一端拿到的是
    一张形容词清单，读起来像个通用助手而不是某个人。
    """
    body = markdown
    m = re.search(r"^#\s*SOUL\s*$", markdown, re.MULTILINE)
    if m:
        body = markdown[m.end():]
    # 自我陈述没有标题，就是第一个 `##` 之前的那几段
    block = re.split(r"^##\s+", body, maxsplit=1, flags=re.MULTILINE)[0].strip()
    if not block:
        return ""
    frame = _TARGET_FRAMES.get(target, target)
    # 「运行于 Hermes Agent框架」这类句子换了端就是错的，必须改写而不是照抄
    block = re.sub(
        r"运行于\s*[^，。,.\n]{0,24}框架",
        f"运行于 {frame} ",
        block,
    )
    block = re.sub(r"你是用户日常使用的主agent", "你是用户日常使用的主 agent", block)
    return block


def build_identity_block(soul_path: Path, target: str = "codex", max_chars: int = 1600) -> str:
    """渲染跨端身份块：自我陈述 + 特质 + 语气（+ 外观设定）。

    与 `extract_soul_excerpt` 的区别：那个只抓「特质 / 语气」两小节，给 Hermes 的
    静态预算用；这个优先给「我是谁」，再补特质与语气，总量受 max_chars 约束。
    """
    if not soul_path.exists():
        return "(SOUL 未配置)"
    try:
        raw = soul_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(SOUL 未配置)"
    if not raw:
        return "(SOUL 未配置)"

    parts: list[str] = []
    intro = _identity_intro(raw, target)
    if intro:
        parts.append(intro)

    traits = _extract_section(raw, r"^###\s*特质")
    if traits:
        parts.append(traits)
    tone = _extract_section(raw, r"^###\s*语气")
    if tone:
        parts.append(tone)

    combined = "\n\n".join(parts).strip()
    if not combined:
        # 兜底：老格式（只有 ## 人格 没有小节）
        persona = _extract_section(raw, r"^##\s*人格")
        combined = persona or raw

    # 外观只在预算还剩很多时才带，它属于「形象」不属于「行为」
    appearance = _extract_section(raw, r"^###\s*外观")
    if appearance and len(combined) + len(appearance) + 2 <= max_chars:
        combined = combined + "\n\n" + appearance

    return _truncate_on_sentence(combined, max_chars)


def _truncate_on_sentence(text: str, max_chars: int) -> str:
    """按句子边界截断，宁可少一句也不要吐半句话。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(c) for c in "。！？\n")
    if cut > max_chars // 3:
        return head[: cut + 1].rstrip()
    return head.rstrip() + "…"


def extract_soul_excerpt(soul_path: Path, max_chars: int = 600) -> str:
    """从 SOUL.md 摘录「人格 + 语气」小节（<=600 字）；缺失则返回占位符。"""
    if not soul_path.exists():
        return "(SOUL 未配置)"
    try:
        raw = soul_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(SOUL 未配置)"
    if not raw:
        return "(SOUL 未配置)"

    traits = _extract_section(raw, r"^###\s*特质")
    tone = _extract_section(raw, r"^###\s*语气")
    if traits or tone:
        combined = "\n\n".join(part for part in (traits, tone) if part)
        return _truncate_text(combined, max_chars)

    persona = _extract_section(raw, r"^##\s*人格")
    if persona:
        return _truncate_text(persona, max_chars)

    return _truncate_text(raw, max_chars)


def read_hot_memory(config: AppConfig, max_chars: int | None = None) -> str:
    """读取 10_Working/HOT.md 正文（剥离 frontmatter，<=1500 字）。"""
    hot_path = config.profile_dir / "10_Working" / "HOT.md"
    if not hot_path.exists():
        return "(HOT.md 未配置)"
    try:
        raw = hot_path.read_text(encoding="utf-8")
    except OSError:
        return "(HOT.md 未配置)"

    body = _FRONTMATTER_RE.sub("", raw, count=1).strip()
    limit = max_chars if max_chars is not None else min(1500, config.memory.hot_max_chars)
    return _truncate_text(body, limit)


def render_boot(config: AppConfig, target: str = "codex") -> dict[str, Any]:
    """按 target 预算渲染纯文本启动块；绝不包含 30_Diary 内容。"""
    clean_target = str(target or "codex").strip().lower()
    inject_cfg = config.memory.inject.get(clean_target) or {}
    if clean_target == "hermes":
        budget = int(inject_cfg.get("static_chars", 450))
    else:
        budget = int(inject_cfg.get("boot_chars", 2400))

    rev = get_current_revision(config)
    header = f"[SHARED_BRAIN profile={config.memory.profile} rev={rev}]"

    if clean_target == "hermes":
        # Hermes 自带 SOUL.md，这里只给一小段摘要，避免同一份人格注入两遍
        soul_text = extract_soul_excerpt(config.broker.soul_path, max_chars=600)
    else:
        # 其它 harness 只靠这个文件认识「我是谁」：必须含自我陈述，不能只给特质清单
        soul_budget = int(inject_cfg.get("soul_chars", 1600))
        soul_text = build_identity_block(config.broker.soul_path, target=clean_target, max_chars=soul_budget)
    hot_text = read_hot_memory(config, max_chars=min(1500, config.memory.hot_max_chars))

    open_tasks = list_open_handoffs(config, limit=3)
    if open_tasks:
        handoff_lines = []
        for t in open_tasks:
            key = str(t.get("task_key", ""))
            status = str(t.get("status", "open"))
            summary = " ".join(str(t.get("summary", "")).split())
            handoff_lines.append(f"- [{status}] {key}: {_truncate_text(summary, 120)}")
        handoff_text = "\n".join(handoff_lines)
    else:
        handoff_text = "(无未关闭接力任务)"

    def _assemble(s_txt: str, h_txt: str, t_txt: str) -> str:
        parts = [header]
        parts.append(f"## 身份\n{s_txt}")
        parts.append(f"## 热记忆\n{h_txt}")
        parts.append(f"## 待续\n{t_txt}")
        return "\n".join(parts)

    rendered = _assemble(soul_text, hot_text, handoff_text)
    truncated = False

    # 超预算按优先级截断：先缩 待续，再缩 身份，最后缩 热记忆（HOT 优先级最高）
    if len(rendered) > budget:
        truncated = True
        excess = len(rendered) - budget
        if len(handoff_text) > 16:
            new_len = max(16, len(handoff_text) - excess)
            handoff_text = _truncate_text(handoff_text, new_len)
            rendered = _assemble(soul_text, hot_text, handoff_text)

    if len(rendered) > budget:
        excess = len(rendered) - budget
        if len(soul_text) > 16:
            new_len = max(16, len(soul_text) - excess)
            soul_text = _truncate_text(soul_text, new_len)
            rendered = _assemble(soul_text, hot_text, handoff_text)

    if len(rendered) > budget:
        overhead = len(_assemble("", "", "")) + len(soul_text) + len(handoff_text)
        avail_hot = max(0, budget - overhead)
        hot_text = _truncate_text(hot_text, avail_hot)
        rendered = _assemble(soul_text, hot_text, handoff_text)

    if len(rendered) > budget:
        rendered = _truncate_text(rendered, budget)

    return {
        "profile": config.memory.profile,
        "target": clean_target,
        "revision": rev,
        "budget": budget,
        "chars": len(rendered),
        "truncated": truncated,
        "text": rendered,
    }


def render_delta(
    config: AppConfig,
    since_rev: int,
    target: str = "codex",
) -> dict[str, Any]:
    """返回自 since_rev 以来的变更摘要；未变化时返回 changed=False。"""
    current_rev = get_current_revision(config)
    if current_rev <= since_rev:
        return {"changed": False, "revision": current_rev}

    inject_cfg = config.memory.inject.get(str(target or "codex").strip().lower()) or {}
    delta_budget = int(inject_cfg.get("delta_chars", 300))

    recent_notes: list[dict[str, str]] = []
    db_path = config.broker.db_path
    if db_path.exists():
        with connect_db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT path, title, type, updated
                FROM notes
                WHERE path NOT LIKE '30_Diary/%'
                ORDER BY indexed_at DESC, id DESC
                LIMIT 5
                """
            ).fetchall()
            for r in rows:
                recent_notes.append(
                    {
                        "path": str(r["path"]),
                        "title": str(r["title"]),
                        "type": str(r["type"] or "note"),
                        "updated": str(r["updated"] or ""),
                    }
                )

    summary_lines = [f"[DELTA rev {since_rev}->{current_rev}]"]
    for n in recent_notes:
        summary_lines.append(f"- {n['title']} ({n['path']})")
    summary = _truncate_text("\n".join(summary_lines), delta_budget)

    return {
        "changed": True,
        "since_rev": since_rev,
        "revision": current_rev,
        "summary": summary,
        "recent_notes": recent_notes,
    }


def render_prefetch(hits: list[dict[str, Any]], max_chars: int = 1200) -> str:
    """将检索命中格式化为紧凑的摘要索引块（[MEMORY_INDEX]，<=max_chars）。

    注：保留函数名与签名，行为已由原先的 [MEMORY_PREFETCH] 正文片段改为只输出
    `- <文件名去 .md> — <summary≤120 字>` 的索引行，不再注入正文 excerpt。
    """
    if not hits:
        return ""
    ordered = sorted(
        hits,
        key=lambda h: (float(h.get("score") or 0.0), str(h.get("updated") or "")),
        reverse=True,
    )
    lines = ["[MEMORY_INDEX]"]
    used = len(lines[0])
    for h in ordered:
        raw_path = str(h.get("path") or "").strip()
        if raw_path:
            name = Path(raw_path).stem
        else:
            raw_title = str(h.get("title") or "").strip()
            name = raw_title[:-3] if raw_title.lower().endswith(".md") else raw_title
        raw_summary = " ".join(str(h.get("summary") or "").split()).strip()
        if not raw_summary:
            raw_summary = " ".join(str(h.get("excerpt") or "").split()).strip()[:100]
        summary = _truncate_text(raw_summary, 120)
        entry = f"- {name} — {summary}"
        if used + len(entry) + 1 > max_chars:
            break
        lines.append(entry)
        used += len(entry) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
