"""§6 小节级时效维护 + §7 摘要索引与记忆工具测试。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agentbrain.acl import hash_token
from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig
from agentbrain.index import reindex
from agentbrain.parse import parse_note
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import validate_and_apply_batch
from agentbrain.proactive_clock import ManualClock
from agentbrain.prompt_lib import load_prompt
from agentbrain.render import render_prefetch
from agentbrain.server import BrokerService, create_http_server


def _make_cfg(tmp_path: Path) -> AppConfig:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    for sub in (
        "10_Working",
        "20_LongTerm/Facts",
        "20_LongTerm/Decisions",
        "20_LongTerm/Entities",
        "20_LongTerm/Projects",
        "20_LongTerm/MOC",
        "30_Diary/Public",
        "30_Diary/Private",
        "90_Archive",
    ):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("# SOUL\n我是 Lily。\n", encoding="utf-8")
    rt = tmp_path / "runtime"
    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18765,
        role="primary",
        index_dir=rt / "index",
        rev_dir=rt / "rev",
        state_dir=rt / "state",
        audit_dir=rt / "audit",
        log_dir=rt / "log",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    mem = MemoryConfig(
        profile="lily",
        clients={"hermes": {"domain": "work"}},
        proactive=ProactiveConfig(enabled=True, event_capture=True, agy_ide_enabled=False),
    )
    return AppConfig(
        broker=broker,
        memory=mem,
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def _seed_job_and_events(cfg: AppConfig, clock: ManualClock) -> tuple[ProactiveStore, Any, list[Any]]:
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="sess-sec",
            event_id=f"e{i}",
            content=f"User: 第 {i} 轮讨论约束\nAssistant: 已记录",
        )
    job = store.claim_job("worker-test")
    assert job is not None
    events = store.get_job_events(job.job_id)
    return store, job, events


def _load_plugin_cls() -> type[Any]:
    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "integrations"
        / "hermes"
        / "obsidian-broker"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("obsidian_broker_plugin_s7", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ObsidianBrokerMemoryProvider


# ---------------------------------------------------------------------------
# §6.5 小节级时效必测
# ---------------------------------------------------------------------------


def test_section_lifecycle_three_writes_and_deprecation_cap(tmp_path: Path) -> None:
    """§6.5：连写多次覆盖同一小节（section_key -> 相似度），废止行上限 2 条，手写段落一字不变。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_job_and_events(cfg, clock)
    ref = events[0].event_id

    # 先写第 1 次：带 section_key="galaxy.rock"、标题「陨石生成硬约束」
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "scope": "project",
                    "project": "Galaxy Attacker",
                    "title": "陨石生成硬约束",
                    "section_key": "galaxy.rock",
                    "content": "岩石净距 ≥40px，走廊 ≥108px。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )

    proj = next((cfg.profile_dir / "20_LongTerm" / "Projects").glob("PROJ-*-Galaxy Attacker.md"))
    # 在受管块外插入一段手写内容，后续所有写入必须一字不变地保留它
    handwritten = "## 手写备忘\n\n这段手写内容绝不能被覆盖或改动：keep-exact-42。\n"
    raw1 = proj.read_text(encoding="utf-8")
    assert "<!-- section_key: galaxy.rock -->" in raw1
    proj.write_text(raw1 + "\n" + handwritten, encoding="utf-8")

    # 第 2 次：换标题（「岩石与关卡的硬性约束」）但同 section_key="galaxy.rock"
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "scope": "project",
                    "project": "Galaxy Attacker",
                    "title": "岩石与关卡的硬性约束",
                    "section_key": "galaxy.rock",
                    "content": "净距 40px、走廊 108px、口袋中心不生成。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    raw2 = proj.read_text(encoding="utf-8")
    headers2 = [ln.strip() for ln in raw2.splitlines() if ln.startswith("### ")]
    assert headers2 == ["### 岩石与关卡的硬性约束"]
    assert "<!-- section_key: galaxy.rock -->" in raw2
    assert "> 已废止（2026-11-21）：岩石净距 ≥40px，走廊 ≥108px。" in raw2
    assert handwritten in raw2

    # 第 3 次：不传 section_key，标题换近义说法（「岩石与关卡硬性约束」，二元组 Jaccard=0.70 >= 0.62）
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "scope": "project",
                    "project": "Galaxy Attacker",
                    "title": "岩石与关卡硬性约束",
                    "content": "净距改为 48px，走廊保持 108px。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    raw3 = proj.read_text(encoding="utf-8")
    headers3 = [ln.strip() for ln in raw3.splitlines() if ln.startswith("### ")]
    assert len(headers3) == 1
    assert "<!-- section_key: galaxy.rock -->" in raw3  # 沿用旧标记
    dep_lines3 = [ln.strip() for ln in raw3.splitlines() if ln.strip().startswith("> 已废止（")]
    assert len(dep_lines3) == 2
    assert "岩石净距 ≥40px，走廊 ≥108px。" in dep_lines3[0]
    assert "净距 40px、走廊 108px、口袋中心不生成。" in dep_lines3[1]
    assert handwritten in raw3

    # 第 4 次：再次更新，验证废止行上限为 2 条（丢弃最旧一条）
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "scope": "project",
                    "project": "Galaxy Attacker",
                    "title": "岩石与关卡硬性约束",
                    "section_key": "galaxy.rock",
                    "content": "最终定稿：净距 52px，走廊 112px。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    raw4 = proj.read_text(encoding="utf-8")
    headers4 = [ln.strip() for ln in raw4.splitlines() if ln.startswith("### ")]
    assert len(headers4) == 1
    dep_lines4 = [ln.strip() for ln in raw4.splitlines() if ln.strip().startswith("> 已废止（")]
    assert len(dep_lines4) == 2
    assert all("岩石净距 ≥40px" not in ln for ln in dep_lines4)  # 最旧一条已丢弃
    assert "净距 40px、走廊 108px、口袋中心不生成。" in dep_lines4[0]
    assert "净距改为 48px，走廊保持 108px。" in dep_lines4[1]
    assert handwritten in raw4


def test_review_system_prompt_has_section_key_and_summary_rules() -> None:
    """§6.4：review_system.md 包含 section_key 与 summary 规则且不含 no_change。"""
    text = load_prompt("review_system")
    assert "section_key" in text
    assert "summary" in text
    assert "no_change" not in text


# ---------------------------------------------------------------------------
# §7.4 摘要索引 + 记忆工具必测
# ---------------------------------------------------------------------------


def test_summary_explicit_derived_and_change_detection(tmp_path: Path) -> None:
    """§7.4-1：显式 summary 写进 frontmatter；未传自动派生且 ≤100 字；仅 summary 变化触发写盘。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_job_and_events(cfg, clock)
    ref = events[0].event_id

    # 1. 显式传入 summary
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "显式摘要测试",
                    "summary": "用于验证显式摘要：关键结论是必须进 frontmatter，排查摘要问题时看它。",
                    "content": "正文内容很长，详细说明前因后果与验证细节。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    f1 = next((cfg.profile_dir / "20_LongTerm" / "Facts").glob("FACT-*-显式摘要测试.md"))
    p1 = parse_note(f1, f1.read_text(encoding="utf-8"))
    assert p1.frontmatter["summary"] == "用于验证显式摘要：关键结论是必须进 frontmatter，排查摘要问题时看它。"

    # 2. 不传 summary，正文超 150 字 -> 自动派生且 <=100 字
    long_body = "这是一段非常长的第一句正文用来验证服务端在未提供摘要时自动从正文首句派生并截断到一百字以内的逻辑" * 3
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "自动派生摘要测试",
                    "content": long_body,
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    f2 = next((cfg.profile_dir / "20_LongTerm" / "Facts").glob("FACT-*-自动派生摘要测试.md"))
    p2 = parse_note(f2, f2.read_text(encoding="utf-8"))
    derived = str(p2.frontmatter.get("summary") or "")
    assert derived
    assert len(derived) <= 100

    # 3. 合并已有文件时未传 summary -> 保留旧 summary
    validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "显式摘要测试",
                    "content": "正文内容更新为第二版。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    p1_after = parse_note(f1, f1.read_text(encoding="utf-8"))
    assert p1_after.frontmatter["summary"] == "用于验证显式摘要：关键结论是必须进 frontmatter，排查摘要问题时看它。"

    # 4. 仅变更 summary（正文不变）-> 必须写盘
    res_sum = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "显式摘要测试",
                    "summary": "更新后的新版摘要，用于确认 summary 变化会触发写盘。",
                    "content": "正文内容更新为第二版。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    assert res_sum.applied_count == 1
    p1_updated = parse_note(f1, f1.read_text(encoding="utf-8"))
    assert p1_updated.frontmatter["summary"] == "更新后的新版摘要，用于确认 summary 变化会触发写盘。"


def test_search_returns_summary_and_render_prefetch_index_format(tmp_path: Path) -> None:
    """§7.4-2：/v1/search 返回 summary；render_prefetch 输出 [MEMORY_INDEX] 格式，不含正文长句，<=max_chars。"""
    cfg = _make_cfg(tmp_path)
    long_detail = "这是一段非常详细的正文长句用于核验不会被直接塞进索引块里导致上下文爆炸。" * 3
    note1 = cfg.profile_dir / "20_LongTerm" / "Facts" / "FACT-01-agy-CLI卡死.md"
    note1.write_text(
        "---\n"
        "type: fact\n"
        "title: agy CLI卡死\n"
        "summary: agy 长任务子进程死后会空等到超时。处理：确认不醒就杀掉接手。\n"
        "updated: 2026-11-21\n"
        "keys: [agy, 子进程]\n"
        "---\n\n"
        f"# agy CLI卡死\n\n{long_detail}\n",
        encoding="utf-8",
    )
    note2 = cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-03-求职-游戏策划.md"
    note2.write_text(
        "---\n"
        "type: project\n"
        "title: 求职-游戏策划\n"
        "updated: 2026-11-20\n"
        "keys: [钛核, 游戏策划]\n"
        "---\n\n"
        "# 求职-游戏策划\n\n系统策划方向；钛核互动面试口径与素材映射都在这。\n",
        encoding="utf-8",
    )
    reindex(cfg)

    svc = BrokerService(cfg)
    res1 = svc.search({"query": "agy 子进程", "k": 5, "hops": 1})
    assert res1["hits"]
    hit1 = res1["hits"][0]
    assert hit1["summary"] == "agy 长任务子进程死后会空等到超时。处理：确认不醒就杀掉接手。"

    res2 = svc.search({"query": "钛核 游戏策划", "k": 5, "hops": 1})
    assert res2["hits"]
    hit2 = res2["hits"][0]
    # 无 frontmatter summary 时退回 excerpt 前 100 字
    assert hit2["summary"]
    assert len(hit2["summary"]) <= 100

    rendered = render_prefetch([hit1, hit2], max_chars=240)
    assert rendered.startswith("[MEMORY_INDEX]\n")
    assert "- FACT-01-agy-CLI卡死 — agy 长任务子进程死后会空等到超时。处理：确认不醒就杀掉接手。" in rendered
    assert long_detail not in rendered
    assert len(rendered) <= 240

    # 验证同分按更新时间倒序
    tied = render_prefetch(
        [
            {"path": "20_LongTerm/Facts/FACT-OLD.md", "summary": "旧条目", "score": 1.5, "updated": "2026-11-19"},
            {"path": "20_LongTerm/Facts/FACT-NEW.md", "summary": "新条目", "score": 1.5, "updated": "2026-11-21"},
        ],
        max_chars=500,
    )
    lines = tied.splitlines()
    assert lines[1].startswith("- FACT-NEW — ")
    assert lines[2].startswith("- FACT-OLD — ")


def test_plugin_tools_schema_search_read_and_security_rejection(tmp_path: Path) -> None:
    """§7.4-3 & 4：插件 schema、memory_search、memory_read、超长截断、30_Diary 拒绝（JSON error）。"""
    cfg = _make_cfg(tmp_path)
    # 准备测试笔记与受限日记
    fact_note = cfg.profile_dir / "20_LongTerm" / "Facts" / "FACT-01-测试规则.md"
    fact_note.write_text(
        "---\n"
        "type: fact\n"
        "title: 测试规则\n"
        "summary: 记录项目测试的核心约束与验证方式。\n"
        "updated: 2026-11-21\n"
        "keys: [测试规则]\n"
        "---\n\n"
        "# 测试规则\n\n所有测试必须运行在 tmp 目录。\n\n## 关联\n- related_to [[PROJ-01-核心项目]]\n",
        encoding="utf-8",
    )
    long_note = cfg.profile_dir / "20_LongTerm" / "Facts" / "FACT-02-超长笔记.md"
    long_note.write_text(
        "---\ntype: fact\ntitle: 超长笔记\nsummary: 用于验证 20000 字截断。\nupdated: 2026-11-21\n---\n\n"
        + ("长" * 21000)
        + "\n",
        encoding="utf-8",
    )
    diary_note = cfg.profile_dir / "30_Diary" / "Public" / "2026-11-21.md"
    diary_note.write_text("# 2026-11-21\n- [10:00:00] 秘密日记\n", encoding="utf-8")
    archive_note = cfg.profile_dir / "90_Archive" / "OLD-01.md"
    archive_note.write_text("# old\n已归档\n", encoding="utf-8")
    reindex(cfg)

    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps({hash_token("tok-test-123"): "hermes"}), encoding="utf-8")

    httpd = create_http_server(cfg, host="127.0.0.1", port=0, tokens_path=tokens_path)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    try:
        provider_cls = _load_plugin_cls()
        provider = provider_cls()
        provider._port = port
        provider._base_url = f"http://127.0.0.1:{port}"
        provider._token = "tok-test-123"

        # 1. get_tool_schemas() 返回 2 个 OpenAI function-calling schema
        schemas = provider.get_tool_schemas()
        assert len(schemas) == 2
        names = [s["function"]["name"] for s in schemas]
        assert names == ["memory_search", "memory_read"]
        assert all(s["type"] == "function" for s in schemas)

        # 协议块包含新规则
        proto = provider.system_prompt_block()
        assert "memory_search / memory_read" in proto
        assert "MEMORY_INDEX" in proto

        # 2. memory_search 返回 JSON 且含 summary
        raw_search = provider.handle_tool_call("memory_search", {"query": "测试规则", "k": 5})
        search_data = json.loads(raw_search)
        assert search_data["total"] >= 1
        assert search_data["hits"][0]["summary"] == "记录项目测试的核心约束与验证方式。"
        assert search_data["hits"][0]["path"] == "20_LongTerm/Facts/FACT-01-测试规则.md"

        # 3. memory_read(title=...) 返回正文与 links
        raw_read = provider.handle_tool_call("memory_read", {"title": "测试规则"})
        read_data = json.loads(raw_read)
        assert read_data["title"] == "测试规则"
        assert read_data["path"] == "20_LongTerm/Facts/FACT-01-测试规则.md"
        assert read_data["summary"] == "记录项目测试的核心约束与验证方式。"
        assert "所有测试必须运行在 tmp 目录。" in read_data["content"]
        assert read_data["updated"] == "2026-11-21"
        assert any(
            lnk["title"] == "PROJ-01-核心项目" and lnk["relation"] == "related_to"
            for lnk in read_data["links"]
        )

        # 4. 超 20000 字截断并标 truncated: true
        raw_long = provider.handle_tool_call("memory_read", {"title": "超长笔记"})
        long_data = json.loads(raw_long)
        assert long_data.get("truncated") is True
        assert len(long_data["content"]) == 20000

        # 5. 禁止访问 30_Diary / 90_Archive / 路径穿越 -> JSON error，不抛异常
        for forbidden_path in (
            "30_Diary/Public/2026-11-21.md",
            "90_Archive/OLD-01.md",
            "../SOUL.md",
        ):
            raw_err = provider.handle_tool_call("memory_read", {"path": forbidden_path})
            err_data = json.loads(raw_err)
            assert "error" in err_data
            assert "秘密日记" not in raw_err
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
