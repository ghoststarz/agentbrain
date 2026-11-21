"""夜间「小节体检」（section audit）单元测试与集成测试。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig, load_memory_config
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_worker import _system_prompt, run_worker_once
from agentbrain.section_audit import (
    apply_audit,
    build_section_audit_prompt,
    discover_audit_candidates,
    parse_audit_response,
    run_section_audit,
)


def _make_cfg(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
    soul_content: str = "---\nname: lily\n---\n# SOUL\nTEST-SOUL-BODY\n",
) -> AppConfig:
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
    soul.write_text(soul_content, encoding="utf-8")
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\ninitial\n", encoding="utf-8")

    rt = tmp_path / "runtime"
    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18799,
        role="primary",
        index_dir=rt / "index",
        rev_dir=rt / "rev",
        state_dir=rt / "state",
        audit_dir=rt / "audit",
        log_dir=rt / "log",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    return AppConfig(
        broker=broker,
        memory=MemoryConfig(
            profile="lily",
            clients={"hermes": {"domain": "work"}},
            proactive=proactive or ProactiveConfig(enabled=True),
        ),
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def _write_project_note(path: Path, managed_body: str, *, handwritten_tail: str = "") -> None:
    content = (
        "---\n"
        "type: project\n"
        "title: Galaxy Attacker\n"
        "updated: '2026-11-20'\n"
        "status: active\n"
        "---\n\n"
        "# Galaxy Attacker\n\n"
        "<!-- agentbrain:managed:start -->\n"
        f"{managed_body.strip()}\n"
        "<!-- agentbrain:managed:end -->\n"
    )
    if handwritten_tail:
        content += "\n" + handwritten_tail
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 必测 1：候选发现 + enabled=False 时整套不跑
# ---------------------------------------------------------------------------


def test_discover_candidates_content_similarity_and_disabled_noop(tmp_path: Path) -> None:
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            section_audit_enabled=False,
            section_audit_min_similarity=0.45,
            section_audit_max_files=3,
        ),
    )

    proj = cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-Galaxy Attacker.md"
    _write_project_note(
        proj,
        managed_body="\n".join(
            [
                "### 陨石生成规则",
                "<!-- section_key: galaxy.rock -->",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石阻塞玩家走位。",
                "",
                "### 关卡障碍摆位",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石阻塞玩家走位，并保证出生点安全。",
                "",
                "### 音频混音参数",
                "",
                "背景音乐默认音量 0.65，爆炸音效上限 4 路并发，低通滤波截止频率 1800Hz。",
                "",
                "### 已全废止旧节",
                "",
                "> 已废止（2026-11-19）：岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石阻塞玩家走位。",
            ]
        ),
    )

    # _Overview.md 必须被忽略
    overview = cfg.profile_dir / "20_LongTerm" / "_Overview.md"
    overview.write_text(
        "<!-- agentbrain:managed:start -->\n### 节一\n\n完全一样的测试内容文本。\n\n### 节二\n\n完全一样的测试内容文本。\n<!-- agentbrain:managed:end -->\n",
        encoding="utf-8",
    )

    cands = discover_audit_candidates(cfg)
    assert len(cands) == 1
    cand = cands[0]
    assert cand["rel"] == "20_LongTerm/Projects/PROJ-01-Galaxy Attacker.md"
    assert cand["title"] == "Galaxy Attacker"

    headings = [s["heading"] for s in cand["sections"]]
    # 全废止小节被排除在候选之外
    assert "已全废止旧节" not in headings
    assert headings == ["陨石生成规则", "关卡障碍摆位", "音频混音参数"]

    # 标题完全不同但正文高度相似的两节被列为候选；不相似的「音频混音参数」不在候选对中
    assert len(cand["pairs"]) == 1
    i, j, score = cand["pairs"][0]
    assert (i, j) == (0, 1)
    assert score >= 0.45

    prompt_text = build_section_audit_prompt(cand)
    assert "陨石生成规则" in prompt_text
    assert "关卡障碍摆位" in prompt_text
    assert "galaxy.rock" in prompt_text

    # section_audit_enabled=False 时整套不跑（不调模型、不写审计文件）
    called = False

    def boom_caller(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("section_audit_enabled=False 时不应调用模型")

    res = run_section_audit(cfg, job_id=99, model_caller=boom_caller)
    assert res == []
    assert called is False
    assert not (cfg.broker.audit_dir / "section_audit.jsonl").exists()


def test_config_defaults_and_yaml_loading(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    pdir.mkdir(parents=True)
    (pdir / "memory.yaml").write_text("schema: 1\nprofile: lily\n", encoding="utf-8")
    mem_default, _ = load_memory_config(vault, profile="lily")
    assert mem_default.proactive.section_audit_enabled is False
    assert mem_default.proactive.section_audit_min_similarity == 0.45
    assert mem_default.proactive.section_audit_max_files == 3

    (pdir / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\nproactive:\n  section_audit_enabled: true\n  section_audit_min_similarity: 0.55\n  section_audit_max_files: 5\n",
        encoding="utf-8",
    )
    mem_custom, _ = load_memory_config(vault, profile="lily")
    assert mem_custom.proactive.section_audit_enabled is True
    assert mem_custom.proactive.section_audit_min_similarity == 0.55
    assert mem_custom.proactive.section_audit_max_files == 5


# ---------------------------------------------------------------------------
# 必测 2：合并应用（两节 -> 一节，已废止行、删除 retire、保留手写段）
# ---------------------------------------------------------------------------


def test_apply_audit_merges_two_sections_and_preserves_handwritten(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    rel = "20_LongTerm/Projects/PROJ-01-Galaxy Attacker.md"
    proj = cfg.profile_dir / rel
    handwritten = "## 手写笔记\n\n这段手写段落必须一字不动：keep-exact-99。\n"
    _write_project_note(
        proj,
        managed_body="\n".join(
            [
                "### 陨石生成硬约束",
                "<!-- section_key: galaxy.rock -->",
                "",
                "岩石净距 >= 40px，走廊宽度 >= 108px。",
                "",
                "### 岩石与关卡的硬性约束",
                "",
                "净距 40px，走廊 108px，\n口袋中心禁止刷岩石。",
                "",
                "### 音频混音参数",
                "",
                "背景音乐默认音量 0.65。",
            ]
        ),
        handwritten_tail=handwritten,
    )

    summaries = apply_audit(
        cfg,
        {
            rel: [
                {
                    "keep": "陨石生成硬约束",
                    "retire": ["岩石与关卡的硬性约束"],
                    "merged_content": "岩石净距 ≥40px，走廊 ≥108px，口袋中心禁止刷岩石。",
                    "section_key": "galaxy.rock.constraints",
                }
            ]
        },
        today_str="2026-11-21",
    )
    assert len(summaries) == 1
    assert summaries[0]["rel"] == rel
    assert summaries[0]["merges"] == [
        {"keep": "陨石生成硬约束", "retired": ["岩石与关卡的硬性约束"], "retired_count": 1}
    ]

    text = proj.read_text(encoding="utf-8")
    assert "### 陨石生成硬约束" in text
    assert "### 岩石与关卡的硬性约束" not in text
    assert "### 音频混音参数" in text
    assert "<!-- section_key: galaxy.rock.constraints -->" in text
    assert "岩石净距 ≥40px，走廊 ≥108px，口袋中心禁止刷岩石。" in text
    assert "> 已废止（2026-11-21）：岩石与关卡的硬性约束：净距 40px，走廊 108px， 口袋中心禁止刷岩石。" in text
    assert handwritten in text


# ---------------------------------------------------------------------------
# 必测 3：模型说 {"merges": []} -> 文件一字不改
# ---------------------------------------------------------------------------


def test_empty_merges_does_not_modify_file(tmp_path: Path) -> None:
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, section_audit_enabled=True),
    )
    rel = "20_LongTerm/Projects/PROJ-01-Galaxy Attacker.md"
    proj = cfg.profile_dir / rel
    _write_project_note(
        proj,
        managed_body="\n".join(
            [
                "### 陨石生成规则",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
                "",
                "### 关卡障碍摆位",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
            ]
        ),
    )
    before = proj.read_text(encoding="utf-8")
    before_mtime_ns = proj.stat().st_mtime_ns

    applied = run_section_audit(
        cfg,
        job_id=7,
        model_caller=lambda *args, **kwargs: {"merges": []},
    )
    assert applied == []
    assert proj.read_text(encoding="utf-8") == before
    assert proj.stat().st_mtime_ns == before_mtime_ns

    # 非法 JSON 输出也跳过且不报错
    assert parse_audit_response("not-json{{{", {"陨石生成规则"}) is None
    applied_invalid = run_section_audit(
        cfg,
        job_id=8,
        model_caller=lambda *args, **kwargs: "not valid json",
    )
    assert applied_invalid == []
    assert proj.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# 必测 4：失败隔离（模型调用抛异常 -> 夜间任务仍 succeeded，写 section_audit_failed 回执）
# ---------------------------------------------------------------------------


def test_nightly_section_audit_failure_is_isolated(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path / "audit_fail",
        proactive=ProactiveConfig(
            enabled=True,
            section_audit_enabled=True,
            section_audit_min_similarity=0.45,
            section_audit_max_files=3,
        ),
    )
    # 放置一个包含候选对的长期笔记，确保 section_audit 会发起模型调用
    proj = cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-Galaxy Attacker.md"
    _write_project_note(
        proj,
        managed_body="\n".join(
            [
                "### 陨石生成规则",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
                "",
                "### 关卡障碍摆位",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
            ]
        ),
    )

    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-sa",
        event_id="sa-1",
        content="白天讨论了夜间小节体检的异常隔离",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    def fake_runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        sys_prompt = str(req.get("system_prompt") or "")
        if "section_audit" in sys_prompt:
            raise RuntimeError("simulated audit model outage")
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "event_date": "2026-11-19",
                        "content": "今天把夜间小节体检的异常隔离接好了，模型挂掉也不会影响复盘成功。",
                        "source_refs": ["sa-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "event_date": "2026-11-19",
                        "content": "夜间收尾多了一道保险，至少小节体检抽风时不会把整晚的复盘拉下水。",
                        "source_refs": ["sa-1"],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=fake_runner)
    assert res is not None and res.outcome == "applied"
    st = store.get_status()
    assert st["jobs"][0]["status"] == "succeeded"

    receipts = store.list_pending_receipts(client_id="hermes", session_id="s-sa")
    assert any(
        r["outcome"] == "section_audit_failed"
        and "RuntimeError" in (r["error_summary"] or "")
        and "simulated audit model outage" in (r["error_summary"] or "")
        for r in receipts
    )


# ---------------------------------------------------------------------------
# 端到端正常路径：夜间复盘 ack 后自动体检并写 audit/section_audit.jsonl
# ---------------------------------------------------------------------------


def test_nightly_section_audit_end_to_end_and_audit_log(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path / "audit_e2e",
        proactive=ProactiveConfig(
            enabled=True,
            section_audit_enabled=True,
            section_audit_min_similarity=0.45,
            section_audit_max_files=3,
        ),
    )
    proj = cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-Galaxy Attacker.md"
    _write_project_note(
        proj,
        managed_body="\n".join(
            [
                "### 陨石生成规则",
                "<!-- section_key: galaxy.rock -->",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
                "",
                "### 关卡障碍摆位",
                "",
                "岩石之间净距至少 40px，通道走廊宽度不小于 108px，禁止在口袋中心刷出岩石。",
            ]
        ),
    )

    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-e2e",
        event_id="e2e-1",
        content="白天讨论了关卡岩石约束合并",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    def fake_runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        sys_prompt = str(req.get("system_prompt") or "")
        if "section_audit" in sys_prompt:
            return {
                "ok": True,
                "content": {
                    "merges": [
                        {
                            "keep": "陨石生成规则",
                            "retire": ["关卡障碍摆位"],
                            "merged_content": "岩石净距 ≥40px，走廊 ≥108px，口袋中心禁止刷岩石。",
                        }
                    ]
                },
            }
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "event_date": "2026-11-19",
                        "content": "今天收口了关卡岩石约束的重复小节。",
                        "source_refs": ["e2e-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "event_date": "2026-11-19",
                        "content": "把讲同一件事的两节合并干净，笔记清爽多了。",
                        "source_refs": ["e2e-1"],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=fake_runner)
    assert res is not None and res.outcome == "applied"
    assert store.get_status()["jobs"][0]["status"] == "succeeded"

    text = proj.read_text(encoding="utf-8")
    assert "### 陨石生成规则" in text
    assert "### 关卡障碍摆位" not in text
    assert "<!-- section_key: galaxy.rock -->" in text
    assert "> 已废止（2026-11-20）：关卡障碍摆位：" in text

    audit_path = cfg.broker.audit_dir / "section_audit.jsonl"
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["job_id"] == res.job_id
    assert len(entry["files"]) == 1
    assert entry["files"][0]["rel"] == "20_LongTerm/Projects/PROJ-01-Galaxy Attacker.md"
    assert entry["files"][0]["merges"][0]["keep"] == "陨石生成规则"


# ---------------------------------------------------------------------------
# 必测 5：_system_prompt("section_audit", soul_text="SOUL-PROBE")
# ---------------------------------------------------------------------------


def test_system_prompt_section_audit_uses_soul_and_asset() -> None:
    prompt = _system_prompt("section_audit", soul_text="SOUL-PROBE")
    assert "SOUL-PROBE" in prompt
    assert "银白长发" not in prompt
    assert "section_audit" in prompt
    assert "拿不准就不要合并" in prompt
    assert "merged_content" in prompt


def test_candidates_ignore_numeric_revisions(tmp_path: Path) -> None:
    """同一条规则改了数值（40px→48px）也必须算候选：差异落在数字上，数字先归一。"""
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, section_audit_enabled=True),
    )
    _write_project_note(
        cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-Demo.md",
        """### 岩石与关卡的硬性约束
<!-- section_key: demo.rock -->
岩石净距至少 40px，陨石速度上限 120，单屏同屏不超过 6 块，避免贴图重叠与击穿判定。

### 陨石生成的硬性约束
岩石净距至少 48px，陨石速度上限 150，单屏同屏不超过 8 块；旧口径已作废，按新数值落地。

### 掉落表
普通关 3 档，精英关 5 档。
""",
    )

    cands = discover_audit_candidates(cfg)
    assert cands, "同一件事的两个数值版本必须进候选"
    pairs = [
        (c["sections"][i]["heading"], c["sections"][j]["heading"])
        for c in cands
        for i, j, _ in c["pairs"]
    ]
    assert ("岩石与关卡的硬性约束", "陨石生成的硬性约束") in pairs or (
        "陨石生成的硬性约束",
        "岩石与关卡的硬性约束",
    ) in pairs
    # 掉落表不该被拉进来
    assert all("掉落表" not in pair for pair in pairs)


def test_parse_audit_response_accepts_fenced_and_prefixed_json() -> None:
    """模型把 JSON 包进 ``` / 前面带一句话时不能静默失效（否则体检永远不合并）。"""
    headings = {"甲", "乙"}
    payload = {"merges": [{"keep": "甲", "retire": ["乙"], "merged_content": "合。"}]}

    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    parsed = parse_audit_response(fenced, headings)
    assert parsed and parsed[0]["keep"] == "甲" and parsed[0]["retire"] == ["乙"]

    chatted = "我看了一下：\n" + json.dumps(payload, ensure_ascii=False) + "\n以上。"
    assert parse_audit_response(chatted, headings) == parsed

    assert parse_audit_response("这些都不重复，不用合并。", headings) is None
