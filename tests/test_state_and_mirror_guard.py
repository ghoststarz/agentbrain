"""note_type=state 固定落点与槽位更新、归档跳过 STATE.md、镜像无命中不新建、提示词改造测试。"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from agentbrain.acl import AuthIdentity
from agentbrain.archive import plan_archive
from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import (
    _LONGTERM_DIRS,
    _SEQ_PREFIX_BY_TYPE,
    _build_new_note_filename,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import ManualClock, dt_to_us
from agentbrain.prompt_lib import load_prompt
from agentbrain.server import BrokerService

IDENT = AuthIdentity(client_id="lily", domain="agent", can_casual=False)
MIRROR_TAGS = ["hermes-mirror", "memory"]


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
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\ninitial\n", encoding="utf-8")
    rt = tmp_path / "runtime"
    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18788,
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
            proactive=ProactiveConfig(enabled=True, archive_enabled=True, archive_after_days=14),
        ),
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def test_state_note_fixed_path_and_in_place_section_replacement(tmp_path: Path) -> None:
    """state 固定落点 Entities/STATE.md，不分配序号，同 section_key 换标题仍就地替换。"""
    assert _LONGTERM_DIRS["state"] == "20_LongTerm/Entities"
    assert "state" not in _SEQ_PREFIX_BY_TYPE
    assert _build_new_note_filename("state", "随便：什么 标题（都不归一化）") == "STATE.md"

    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 27, 10, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-state", event_id=f"st-{i}", content=f"turn {i}")
    job = store.claim_job("w-state")
    assert job is not None
    events = store.get_job_events(job.job_id)

    # 第一次写入：创建 20_LongTerm/Entities/STATE.md，标题写进正文 H1
    res1 = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "state",
                    "title": "眼下的状态",
                    "section_key": "状态.现在",
                    "summary": "刚结束实习首日",
                    "content": "刚结束公司实习第一天，晚上回来整理记忆系统。",
                    "source_refs": ["st-1"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "state",
                    "title": "这几天近况",
                    "section_key": "状态.这几天",
                    "summary": "开始游戏策划实习",
                    "content": "这几天在熟悉公司旧策划文档，白天不能用个人设备。",
                    "source_refs": ["st-2"],
                },
            ],
        },
        clock=clock,
    )
    assert res1.applied_count == 2
    state_file = cfg.profile_dir / "20_LongTerm" / "Entities" / "STATE.md"
    assert state_file.exists()
    entity_files = [p.name for p in (cfg.profile_dir / "20_LongTerm" / "Entities").glob("*.md")]
    assert entity_files == ["STATE.md"]
    text1 = state_file.read_text(encoding="utf-8")
    assert "# 眼下的状态" in text1
    assert "<!-- section_key: 状态.现在 -->" in text1
    assert "<!-- section_key: 状态.这几天 -->" in text1

    # 第二次写入：换标题但同 section_key="状态.现在"，命中已有 STATE.md 并就地替换，保留废止行
    res2 = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "state",
                    "title": "此刻的心情与状态",
                    "section_key": "状态.现在",
                    "summary": "晚上写完测试心情轻松",
                    "content": "晚上把 state 改造和测试跑通了，精神还不错。",
                    "source_refs": ["st-3"],
                }
            ],
        },
        clock=clock,
    )
    assert res2.applied_count == 1
    entity_files2 = [p.name for p in (cfg.profile_dir / "20_LongTerm" / "Entities").glob("*.md")]
    assert entity_files2 == ["STATE.md"]
    text2 = state_file.read_text(encoding="utf-8")
    assert "晚上把 state 改造和测试跑通了，精神还不错。" in text2
    assert "> 已废止（2026-11-27）：刚结束公司实习第一天，晚上回来整理记忆系统。" in text2
    # 另一个槽位原样保留
    assert "<!-- section_key: 状态.这几天 -->" in text2
    assert "这几天在熟悉公司旧策划文档" in text2


def test_archive_never_touches_state_page(tmp_path: Path) -> None:
    """即使 STATE.md 超过 14 天未修改且无访问记录，plan_archive 也不得将其纳入归档候选。"""
    now_utc = dt.datetime(2026, 11, 27, 15, 0, tzinfo=dt.timezone.utc)
    now_us = dt_to_us(now_utc)
    old_ts = now_utc.timestamp() - 30 * 86400
    recent_ts = now_utc.timestamp() - 1 * 86400

    cfg = _make_cfg(tmp_path)
    hot_file = cfg.profile_dir / "10_Working" / "HOT.md"
    os.utime(hot_file, (recent_ts, recent_ts))

    entities_dir = cfg.profile_dir / "20_LongTerm" / "Entities"
    state_file = entities_dir / "STATE.md"
    old_entity = entities_dir / "ENTITY-旧设备.md"
    state_file.write_text("---\ntype: state\ntitle: 状态\n---\n# 状态\nold state\n", encoding="utf-8")
    old_entity.write_text("---\ntype: entity\ntitle: 旧设备\n---\n# 旧设备\nold entity\n", encoding="utf-8")
    os.utime(state_file, (old_ts, old_ts))
    os.utime(old_entity, (old_ts, old_ts))

    candidates = plan_archive(cfg, now_us, days=14)
    rel_paths = [c.rel_path for c in candidates]
    assert "20_LongTerm/Entities/STATE.md" not in rel_paths
    assert "20_LongTerm/Entities/ENTITY-旧设备.md" in rel_paths


def test_mirror_upsert_no_target_skips_without_creating_file(isolated_env: AppConfig) -> None:
    """upsert=True 且带 hermes-mirror 且未命中已有镜像笔记时，不新建文件并返回 skipped。"""
    svc = BrokerService(isolated_env)
    facts_dir = isolated_env.profile_dir / "20_LongTerm" / "Facts"

    res = svc.write_note(
        {
            "content": "一条找不到对应目标的镜像记忆正文。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "不存在的镜像目标",
            "upsert": True,
        },
        identity=IDENT,
    )
    assert res["ok"] is True
    assert res["skipped"] == "mirror_no_target"
    assert res["path"] is None
    assert list(facts_dir.glob("*.md")) == []


def test_mirror_upsert_hits_existing_mirror_note_in_place(isolated_env: AppConfig) -> None:
    """镜像命中已有带 hermes-mirror 标签的笔记时，仍原地覆盖。"""
    svc = BrokerService(isolated_env)
    facts_dir = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    seed = facts_dir / "已有镜像.md"
    seed.write_text(
        "---\ntype: fact\ntitle: 已有镜像\ncreated: 2026-11-01\ntags: [\"hermes-mirror\", \"memory\"]\nstatus: active\n---\n\n# 已有镜像\n\n旧正文。\n",
        encoding="utf-8",
    )

    res = svc.write_note(
        {
            "content": "更新后的镜像正文。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "已有镜像",
            "upsert": True,
            "previous_content": "旧正文。",
        },
        identity=IDENT,
    )
    assert res["ok"] is True
    assert "skipped" not in res
    assert res["path"] == "20_LongTerm/Facts/已有镜像.md"
    assert "更新后的镜像正文。" in seed.read_text(encoding="utf-8")
    assert len(list(facts_dir.glob("*.md"))) == 1


def test_handwritten_write_still_creates_new_file(isolated_env: AppConfig) -> None:
    """不带 hermes-mirror 的手写写入语义完全不变，仍可正常新建文件。"""
    svc = BrokerService(isolated_env)
    facts_dir = isolated_env.profile_dir / "20_LongTerm" / "Facts"

    res1 = svc.write_note(
        {
            "content": "手写普通事实正文。",
            "type": "fact",
            "tags": ["manual"],
            "title": "手写新建测试",
            "upsert": True,
        },
        identity=IDENT,
    )
    assert res1["ok"] is True
    assert "skipped" not in res1
    assert res1["path"] == "20_LongTerm/Facts/手写新建测试.md"
    assert (facts_dir / "手写新建测试.md").exists()


def test_prompt_assets_cover_fact_pages_and_state_slots() -> None:
    """提示词覆盖三本大类 FACT 页（按页标题寻址，不绑文件名）、可扩充新页、四类 note_type、状态槽位且不含 no_change。"""
    sys_text = load_prompt("review_system")
    # 用页标题寻址：vault 里的文件名带序号，序号会变（用户手改过 15/16/17 → 01/02/03），
    # 提示词里写死文件名就会在下一次重排后指向不存在的页。
    for token in ("`工作`", "`日常`", "`创意`", "merge_into", "状态.现在", "状态.这几天"):
        assert token in sys_text
    assert "新开一本" in sys_text
    assert "no_change" not in sys_text
    for nt in ('"fact"', '"decision"', '"entity"', '"state"'):
        assert nt in sys_text

    for name in (
        "review_nightly_addendum",
        "review_nightly_diary",
        "review_nightly_idle",
        "review_nightly_work",
        "review_nightly_segment",
    ):
        text = load_prompt(name)
        first_line = text.splitlines()[0]
        assert first_line.startswith("你是 Lily 本人"), f"{name} 首句缺少身份声明"
        assert "状态.现在" in text and "状态.这几天" in text, f"{name} 缺少 state 槽位提醒"
