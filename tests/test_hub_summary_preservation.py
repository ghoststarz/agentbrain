"""大类页/项目页的页级 summary 不许被单个小节的摘要顶掉。"""

from __future__ import annotations

from agentbrain.proactive_apply import _format_longterm_note_content

HUB = """---
type: fact
title: 工作
summary: "这一页是干什么的：跨项目工具链路与环境事实"
proactive_owned: true
created: '2026-11-27'
updated: '2026-11-27'
source: hermes
domain: work
status: active
evidence_level: report
---

# 工作

<!-- agentbrain:managed:start -->
### 中文检索
<!-- section_key: 工作.中文检索 -->

FTS5 默认分词对二字词 0 命中。

### 远程接入
<!-- section_key: 工作.远程接入现状 -->

192.168.1.11 免密可连。

## 关联
- related_to [[PROJ-01-AgentBrain-记忆系统]]
<!-- agentbrain:managed:end -->
"""


def _render(existing: str, summary: str, section_key: str = "工作.agy") -> str:
    return _format_longterm_note_content(
        existing_text=existing,
        note_type="fact",
        title="AGY 卡死",
        content="长任务的子进程死后会空等。",
        keys=["agy"],
        tags=[],
        fact_key=None,
        evidence_level="report",
        source_refs=[],
        trusted_source="hermes",
        trusted_domain="work",
        today_str="2026-11-27",
        as_section=True,
        section_key=section_key,
        summary=summary,
    )


def test_hub_page_summary_survives_section_merge() -> None:
    out = _render(HUB, "小节摘要：agy 会卡在已死子进程")
    assert "summary: 这一页是干什么的：跨项目工具链路与环境事实" in out
    assert "小节摘要" not in out.split("---", 2)[1]
    # 小节本身照样落地
    assert "<!-- section_key: 工作.agy -->" in out
    assert "长任务的子进程死后会空等。" in out


def test_single_section_note_summary_still_refreshes() -> None:
    single = HUB.replace("""### 远程接入
<!-- section_key: 工作.远程接入现状 -->

192.168.1.11 免密可连。
""", "")
    out = _render(single, "新的单节摘要：agy 卡死")
    fm = out.split("---", 2)[1]
    assert "新的单节摘要" in fm
