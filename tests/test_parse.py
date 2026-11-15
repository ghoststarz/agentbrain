"""parse.py 单元测试：Frontmatter 契约与全量双链/类型化关系语法。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentbrain.parse import parse_note


def test_frontmatter_contract_all_fields() -> None:
    content = """---
type: decision
created: 2026-11-14
updated: 2026-11-14
source: user
tags: [adr, sync]
confidence: 0.95
status: active
pinned: true
supersedes: "[[ADR-01]]"
superseded_by: null
last_activated: 2026-11-14
access_count: 3
decided_at: 2026-11-14
context: 同步器定位
alternatives: [无中枢]
fact_key: 记忆库.架构
valid_from: 2026-11-14
valid_to: null
episode: 15_Daily/2026/2026-11-14#^sess-ab12
category: stable_profile
---
# ADR-03

正文内容
"""
    note = parse_note(Path("ADR-03.md"), content)
    fm = note.frontmatter
    assert fm["type"] == "decision"
    assert fm["created"] == "2026-11-14"
    assert fm["fact_key"] == "记忆库.架构"
    assert fm["valid_from"] == "2026-11-14"
    assert fm["valid_to"] is None
    assert fm["episode"] == "15_Daily/2026/2026-11-14#^sess-ab12"
    assert fm["category"] == "stable_profile"
    assert fm["pinned"] is True
    assert fm["supersedes"] == "[[ADR-01]]"


@pytest.mark.parametrize(
    ("line", "expected_dst", "expected_anchor", "expected_embed", "expected_rel"),
    [
        ("普通链 [[笔记A]]", "笔记A", None, 0, "links_to"),
        ("别名链 [[笔记B|显示名]]", "笔记B", None, 0, "links_to"),
        ("小节链 [[笔记C#核心结论]]", "笔记C", "核心结论", 0, "links_to"),
        ("块链 [[笔记D#^blk-01]]", "笔记D", "^blk-01", 0, "links_to"),
        ("嵌入 ![[笔记E]]", "笔记E", None, 1, "links_to"),
        ("相对 [文字](subdir/目标笔记.md)", "目标笔记", None, 0, "links_to"),
        ("- requires [[前置决策]]", "前置决策", None, 0, "requires"),
        ("- [[普通列表项]]", "普通列表项", None, 0, "links_to"),
        ("行内提到 [[行内笔记]] 的设计", "行内笔记", None, 0, "links_to"),
    ],
)
def test_all_link_syntaxes(
    line: str,
    expected_dst: str,
    expected_anchor: str | None,
    expected_embed: int,
    expected_rel: str,
) -> None:
    note = parse_note(Path("Test.md"), f"# Test\n{line}\n")
    assert len(note.links) == 1
    link = note.links[0]
    assert link.dst_title == expected_dst
    assert link.anchor == expected_anchor
    assert link.embed == expected_embed
    assert link.relation == expected_rel
    assert link.context_line == line


def test_ignored_non_note_embeds_and_external_links() -> None:
    content = """# Test
![[图.png]]
![[架构图.webp]]
[官网](https://example.com/a.md)
[附件](doc/spec.pdf)
"""
    note = parse_note(Path("Test.md"), content)
    assert note.links == []
