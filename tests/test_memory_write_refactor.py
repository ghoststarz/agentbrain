"""记忆写入结构重构测试：序号命名、合并与 merge_into、项目归并、双链、空窗口夜间日记、召回审计与噪声抑制。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig, RetrievalConfig
from agentbrain.index import reindex
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import (
    MANAGED_END,
    MANAGED_START,
    _extract_managed_block,
    _parse_frontmatter_and_body,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_worker import _nightly_system_prompt_overhead_chars, run_worker_once
from agentbrain.server import BrokerService


def _make_cfg(
    tmp_path: Path,
    *,
    nightly_always_diary: bool = False,
    prefetch_min_score: float = 1.0,
) -> AppConfig:
    vault_root = tmp_path / "vault"
    profile_dir = vault_root / "profiles" / "lily"
    for sub in (
        "10_Working/.state",
        "20_LongTerm/Facts",
        "20_LongTerm/Decisions",
        "20_LongTerm/Entities",
        "20_LongTerm/MOC",
        "20_LongTerm/Projects",
        "30_Diary/Public",
        "30_Diary/Private",
        "90_Archive",
    ):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Lily\n你是 Lily，20 岁，说话干脆带点冷幽默。\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    broker = BrokerConfig(
        vault_root=vault_root,
        soul_path=soul,
        port=18765,
        role="primary",
        index_dir=runtime / "index",
        rev_dir=runtime / "rev",
        state_dir=runtime / "state",
        audit_dir=runtime / "audit",
        log_dir=runtime / "logs",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    proactive = ProactiveConfig(
        enabled=True,
        event_capture=True,
        incremental_batch_size=2,
        nightly_hour=22,
        nightly_minute=30,
        model_backend="hermes",
        nightly_always_diary=nightly_always_diary,
        prefetch_min_score=prefetch_min_score,
    )
    memory = MemoryConfig(
        profile="lily",
        clients={"hermes": {"domain": "work"}, "antigravity": {"domain": "work"}},
        retrieval=RetrievalConfig(),
        proactive=proactive,
    )
    return AppConfig(
        broker=broker,
        memory=memory,
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def _seed_claimed_job(
    cfg: AppConfig,
    clock: ManualClock,
    *,
    session_id: str = "s1",
    event_ids: tuple[str, ...] = ("e1", "e2"),
) -> tuple[ProactiveStore, Any, list[Any]]:
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for idx, eid in enumerate(event_ids, start=1):
        store.ingest_event(
            client="hermes",
            session_id=session_id,
            event_id=eid,
            content=f"User: event {idx}\nAssistant: ack {idx}",
            meta={"verified": True} if idx == len(event_ids) else None,
        )
    job = store.claim_job("w-test")
    assert job is not None
    return store, job, store.get_job_events(job.job_id)


def test_1_seq_numbering_follows_live_dir_not_archive(tmp_path: Path) -> None:
    """需求 A：序号只看活目录（已有 01/07 → 新的是 08、09）；归档不动活目录编号。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    dec_dir = cfg.profile_dir / "20_LongTerm" / "Decisions"
    (dec_dir / "ADR-01-旧架构决定.md").write_text("---\ntype: decision\ntitle: 旧架构决定\n---\n# 旧架构决定\n", encoding="utf-8")
    (dec_dir / "ADR-07-旧缓存决定.md").write_text("---\ntype: decision\ntitle: 旧缓存决定\n---\n# 旧缓存决定\n", encoding="utf-8")
    # 夹杂旧式 8 位日期文件不应污染两位序号
    (dec_dir / "ADR-20261119-旧日期格式.md").write_text("---\ntype: decision\ntitle: 旧日期格式\n---\n# 旧日期格式\n", encoding="utf-8")

    store, job, events = _seed_claimed_job(cfg, clock, session_id="s-seq", event_ids=("e1", "e2"))
    summary = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "decision",
                    "title": "采用序号命名",
                    "content": "新建决策统一使用 ADR-NN 序号。",
                    "links": ["ADR-07-旧缓存决定"],
                    "source_refs": ["e1"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "decision",
                    "title": "禁止复用旧号",
                    "content": "即使旧文件归档也不释放序号。",
                    "links": ["ADR-08-采用序号命名"],
                    "source_refs": ["e2"],
                },
            ],
        },
        clock=clock,
    )
    assert summary.applied_count == 2
    adr08 = dec_dir / "ADR-08-采用序号命名.md"
    adr09 = dec_dir / "ADR-09-禁止复用旧号.md"
    assert adr08.exists()
    assert adr09.exists()

    # 将 ADR-09 移入 90_Archive：编号只看活目录，所以下一条又回到 09（归档不占用编号）
    archived_dir = cfg.profile_dir / "90_Archive" / "20_LongTerm" / "Decisions"
    archived_dir.mkdir(parents=True, exist_ok=True)
    adr09.rename(archived_dir / adr09.name)

    store.ack_job_success(job.job_id, job.lease_token, outcome="applied")
    store.ingest_event(client="hermes", session_id="s-seq", event_id="e3", content="User: third\nAssistant: ok")
    store.ingest_event(client="hermes", session_id="s-seq", event_id="e4", content="User: fourth\nAssistant: ok")
    job2 = store.claim_job("w-test")
    assert job2 is not None
    events2 = store.get_job_events(job2.job_id)
    validate_and_apply_batch(
        cfg,
        store,
        job2,
        events2,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "decision",
                    "title": "归档后重新可用",
                    "content": "活目录最大是 08，所以拿到 09。",
                    "source_refs": ["e3"],
                }
            ],
        },
        clock=clock,
    )
    assert (dec_dir / "ADR-09-归档后重新可用.md").exists()
    assert not (dec_dir / "ADR-10-归档后重新可用.md").exists()


def test_2_merge_same_title_appends_section_and_preserves_handwritten(tmp_path: Path) -> None:
    """需求 C：同标题第二次写入不新建文件，受管区块新增 ### 小节，手写段落原样保留。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_claimed_job(cfg, clock, session_id="s-merge", event_ids=("m1", "m2"))

    # 第一次新建
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
                    "title": "缓存淘汰策略",
                    "content": "默认采用 LRU 淘汰。",
                    "keys": ["缓存", "LRU"],
                    "evidence_level": "report",
                    "source_refs": ["m1"],
                }
            ],
        },
        clock=clock,
    )
    fact_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    created = fact_dir / "FACT-01-缓存淘汰策略.md"
    assert created.exists()

    # 在受管区块之外追加手写段落
    handwritten = "\n## 我的手写笔记\n这段手写内容绝对不能被覆盖或删除。\n"
    created.write_text(created.read_text(encoding="utf-8") + handwritten, encoding="utf-8")

    # 第二次同标题写入（含更高 evidence_level 和新 keys）
    store.ack_job_success(job.job_id, job.lease_token, outcome="applied")
    store.ingest_event(client="hermes", session_id="s-merge", event_id="m3", content="User: update\nAssistant: ok")
    store.ingest_event(
        client="hermes",
        session_id="s-merge",
        event_id="m4",
        content="User: verified\nAssistant: ok",
        meta={"verified": True},
    )
    job2 = store.claim_job("w-test")
    assert job2 is not None
    events2 = store.get_job_events(job2.job_id)

    validate_and_apply_batch(
        cfg,
        store,
        job2,
        events2,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "缓存淘汰策略",
                    "content": "补充：热点 key 单独设置 TTL。",
                    "keys": ["LRU", "TTL"],
                    "evidence_level": "verified",
                    "source_refs": ["m4"],
                }
            ],
        },
        clock=clock,
    )
    # 不新建第二个文件
    assert [p.name for p in fact_dir.glob("*.md")] == ["FACT-01-缓存淘汰策略.md"]

    text = created.read_text(encoding="utf-8")
    assert "这段手写内容绝对不能被覆盖或删除。" in text
    managed = _extract_managed_block(text)
    assert managed is not None
    assert "默认采用 LRU 淘汰。" in managed
    assert "### 缓存淘汰策略" in managed
    assert "补充：热点 key 单独设置 TTL。" in managed

    fm, _ = _parse_frontmatter_and_body(text)
    assert fm["keys"] == ["缓存", "LRU", "TTL"]
    assert fm["evidence_level"] == "verified"
    assert set(fm["source_refs"]) == {"m1", "m4"}


def test_3_merge_into_appends_to_specified_target(tmp_path: Path) -> None:
    """需求 C：op 指定 merge_into 时追加小节到目标文件，不新建文件。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_claimed_job(cfg, clock, session_id="s-into", event_ids=("i1", "i2"))

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
                    "title": "数据库选型",
                    "content": "核心索引使用 SQLite WAL 模式。",
                    "source_refs": ["i1"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "连接超时约定",
                    "merge_into": "数据库选型",
                    "content": "busy_timeout 统一设 5000ms。",
                    "source_refs": ["i2"],
                },
            ],
        },
        clock=clock,
    )
    fact_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    files = sorted(p.name for p in fact_dir.glob("*.md"))
    assert files == ["FACT-01-数据库选型.md"]
    managed = _extract_managed_block((fact_dir / "FACT-01-数据库选型.md").read_text(encoding="utf-8"))
    assert managed is not None
    assert "核心索引使用 SQLite WAL 模式。" in managed
    assert "### 连接超时约定" in managed
    assert "busy_timeout 统一设 5000ms。" in managed


def test_4_project_scope_routing_and_section_replace_and_project_update_coexist(tmp_path: Path) -> None:
    """需求 B：scope=project 路由到 PROJ-<NN>-<项目名>.md，Facts 不增文件，同标题替换，与 project_update 互不破坏。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_claimed_job(cfg, clock, session_id="s-proj", event_ids=("p1", "p2"))

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
                    "title": "弹幕冷却参数",
                    "content": "初版冷却 0.8 秒。",
                    "links": ["MOC-游戏设计"],
                    "source_refs": ["p1"],
                }
            ],
        },
        clock=clock,
    )
    fact_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    proj_file = cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-Galaxy Attacker.md"
    assert list(fact_dir.glob("*.md")) == []
    assert proj_file.exists()

    # 追加手写内容
    proj_file.write_text(proj_file.read_text(encoding="utf-8") + "\n手写备忘：别动我。\n", encoding="utf-8")

    # 同一标题再写：就地替换而不重复追加；同时发 project_update，互不破坏
    store.ack_job_success(job.job_id, job.lease_token, outcome="applied")
    store.ingest_event(client="hermes", session_id="s-proj", event_id="p3", content="User: p3\nAssistant: ok")
    store.ingest_event(client="hermes", session_id="s-proj", event_id="p4", content="User: p4\nAssistant: ok")
    job2 = store.claim_job("w-test")
    assert job2 is not None
    events2 = store.get_job_events(job2.job_id)

    validate_and_apply_batch(
        cfg,
        store,
        job2,
        events2,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "scope": "project",
                    "project": "Galaxy Attacker",
                    "title": "弹幕冷却参数",
                    "content": "调整后冷却 0.5 秒，手感更紧凑。",
                    "source_refs": ["p3"],
                },
                {
                    "op_type": "project_update",
                    "project": "Galaxy Attacker",
                    "managed_content": "当前阶段：核心战斗原型调优。",
                    "source_refs": ["p4"],
                },
            ],
        },
        clock=clock,
    )
    assert list(fact_dir.glob("*.md")) == []
    text = proj_file.read_text(encoding="utf-8")
    assert "手写备忘：别动我。" in text
    managed = _extract_managed_block(text)
    assert managed is not None
    assert "当前阶段：核心战斗原型调优。" in managed
    assert managed.count("### 弹幕冷却参数") == 1
    assert "调整后冷却 0.5 秒，手感更紧凑。" in managed
    # §6.3 新契约：覆盖时不丢旧结论，降级为「已废止」行保留（便于回溯"当时为什么这么定"）
    assert "> 已废止（" in managed and "初版冷却 0.8 秒。" in managed
    assert "## 关联" in managed
    assert "- related_to [[MOC-游戏设计]]" in managed


def test_5_wikilinks_dedupe_and_preserve_handwritten_links(tmp_path: Path) -> None:
    """需求 D：受管区块维护 ## 关联 与 - related_to [[…]]，不重复且不删除手写链接行。"""
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store, job, events = _seed_claimed_job(cfg, clock, session_id="s-link", event_ids=("l1", "l2"))

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
                    "title": "同步器协议",
                    "content": "所有写入走 HTTP broker。",
                    "links": ["PROJ-mem-broker", "ADR-01"],
                    "source_refs": ["l1"],
                }
            ],
        },
        clock=clock,
    )
    note = cfg.profile_dir / "20_LongTerm" / "Facts" / "FACT-01-同步器协议.md"
    assert note.exists()
    managed1 = _extract_managed_block(note.read_text(encoding="utf-8"))
    assert managed1 is not None
    assert "## 关联" in managed1
    assert "- related_to [[PROJ-mem-broker]]" in managed1
    assert "- related_to [[ADR-01]]" in managed1

    # 在受管区块的 ## 关联 末尾手写一行链接
    raw_text = note.read_text(encoding="utf-8")
    raw_text = raw_text.replace(
        MANAGED_END,
        "- depends_on [[手写参考页]]\n" + MANAGED_END,
    )
    note.write_text(raw_text, encoding="utf-8")

    # 第二次写入包含重复链接 + 新链接
    store.ack_job_success(job.job_id, job.lease_token, outcome="applied")
    store.ingest_event(client="hermes", session_id="s-link", event_id="l3", content="User: l3\nAssistant: ok")
    store.ingest_event(client="hermes", session_id="s-link", event_id="l4", content="User: l4\nAssistant: ok")
    job2 = store.claim_job("w-test")
    assert job2 is not None
    events2 = store.get_job_events(job2.job_id)

    validate_and_apply_batch(
        cfg,
        store,
        job2,
        events2,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "同步器协议",
                    "content": "补充：端口默认 18765。",
                    "links": ["PROJ-mem-broker", "MOC-记忆系统"],
                    "source_refs": ["l3"],
                }
            ],
        },
        clock=clock,
    )
    managed2 = _extract_managed_block(note.read_text(encoding="utf-8"))
    assert managed2 is not None
    assert managed2.count("[[PROJ-mem-broker]]") == 1
    assert "- depends_on [[手写参考页]]" in managed2
    assert "- related_to [[MOC-记忆系统]]" in managed2


def test_6_idle_nightly_always_diary_true_vs_false(tmp_path: Path) -> None:
    """需求 E：nightly_always_diary=True 空窗口建任务并写公私账；False 时 skipped_empty 不建任务。"""
    tz = dt.timezone(dt.timedelta(hours=8))

    # 场景 1：nightly_always_diary=True
    clock1 = ManualClock(dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg_on = _make_cfg(tmp_path / "on", nightly_always_diary=True)
    store_on = ProactiveStore.from_app_config(cfg_on, clock=clock1)
    # 推进到 2026-11-20 22:35 本地时间（14:35 UTC），2026-11-20 窗口关闭且无事件
    clock1.set_utc(dt.datetime(2026, 11, 20, 14, 35, tzinfo=dt.timezone.utc))
    tick_on = store_on.tick()
    assert tick_on.jobs_created == 1
    assert tick_on.windows_skipped_empty == 0

    seen_prompts: list[tuple[str, str]] = []

    def fake_idle_runner(
        *,
        bridge_path: Path,
        request: dict[str, Any],
        env: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        seen_prompts.append((str(request["system_prompt"]), str(request["user_prompt"])))
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "diary_append",
                    "ledger": "public",
                    "content": "今天很安静，没有新对话进来，我自己整理了一下最近的笔记结构，把几条零散的思路理顺了，感觉清爽了不少。",
                    "event_date": "2026-11-20",
                    "source_refs": [],
                },
                {
                    "op_type": "diary_append",
                    "ledger": "private",
                    "content": "难得一整天安安静静的，没有消息打断，倒挺适合把之前没想透的架构细节慢慢复盘一遍。",
                    "event_date": "2026-11-20",
                    "source_refs": [],
                },
            ],
        }

    # 预算估算应覆盖三种夜间提示词
    assert _nightly_system_prompt_overhead_chars(window_id="2026-11-20", soul_path=cfg_on.broker.soul_path) > 0

    res = run_worker_once(cfg_on, store_on, clock=clock1, hermes_runner=fake_idle_runner)
    assert res is not None
    assert res.outcome == "applied"
    assert res.applied_count == 2
    assert len(seen_prompts) == 1
    assert "严禁编造用户当天做过什么" in seen_prompts[0][0]
    assert "本窗口没有任何新对话" in seen_prompts[0][1]

    pub_diary = cfg_on.profile_dir / "30_Diary" / "Public" / "2026-11-20.md"
    pri_diary = cfg_on.profile_dir / "30_Diary" / "Private" / "2026-11-20.md"
    assert pub_diary.exists()
    assert pri_diary.exists()
    assert "今天很安静" in pub_diary.read_text(encoding="utf-8")
    assert "难得一整天安安静静的" in pri_diary.read_text(encoding="utf-8")

    status_on = store_on.get_status()
    assert len(status_on["jobs"]) == 1
    assert status_on["jobs"][0]["status"] == "succeeded"

    # 场景 2：nightly_always_diary=False
    clock2 = ManualClock(dt.datetime(2026, 11, 20, 10, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg_off = _make_cfg(tmp_path / "off", nightly_always_diary=False)
    store_off = ProactiveStore.from_app_config(cfg_off, clock=clock2)
    clock2.set_utc(dt.datetime(2026, 11, 20, 14, 35, tzinfo=dt.timezone.utc))
    tick_off = store_off.tick()
    assert tick_off.jobs_created == 0
    assert tick_off.windows_skipped_empty == 1


def test_7_recall_audit_and_inject_noise_suppression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """需求 F：/v1/search 写 recall.jsonl 审计；inject=True 丢弃低于阈值的命中，不带 inject 行为不变。"""
    cfg = _make_cfg(tmp_path, prefetch_min_score=9.9)
    facts_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    (facts_dir / "FACT-01-量子咖啡机.md").write_text(
        "---\ntype: fact\ntitle: 量子咖啡机\nstatus: active\nkeys: [\"量子咖啡机\"]\n---\n\n"
        "# 量子咖啡机\n\n量子咖啡机使用低温萃取工艺。\n",
        encoding="utf-8",
    )
    reindex(cfg)
    svc = BrokerService(cfg)

    # 1) 不带 inject：命中非空
    res_plain = svc.search(
        {"query": "量子咖啡机 sk-secret1234567890abcdef1234", "k": 5},
        client_id="cli_test",
    )
    assert len(res_plain["hits"]) >= 1

    # 2) 带 inject=True 且全部命中 score < 9.9：命中被抑制为空
    res_inject = svc.search(
        {"query": "量子咖啡机", "k": 5, "session_id": "sess-1", "inject": True},
        client_id="hermes",
    )
    assert res_inject["hits"] == []
    assert res_inject["truncated"] is False

    # 校验 recall.jsonl 审计文件
    recall_log = cfg.broker.audit_dir / "recall.jsonl"
    assert recall_log.exists()
    rows = [json.loads(line) for line in recall_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2

    expected_keys = {"ts", "client", "session", "query", "hits", "top_score", "weak", "injected", "revision"}
    for row in rows:
        assert expected_keys.issubset(row.keys())

    assert rows[0]["client"] == "cli_test"
    assert rows[0]["session"] == ""
    assert "sk-secret" not in rows[0]["query"]
    assert rows[0]["hits"] >= 1
    assert rows[0]["injected"] is True

    assert rows[1]["client"] == "hermes"
    assert rows[1]["session"] == "sess-1"
    assert rows[1]["hits"] == 0
    assert rows[1]["injected"] is False

    # 3) 验证插件 prefetch() 发送的 payload 带 inject=True
    plugin_path = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "obsidian-broker" / "__init__.py"
    spec = importlib.util.spec_from_file_location("obsidian_broker_plugin_inject_test", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    provider = mod.ObsidianBrokerMemoryProvider()
    captured: list[dict[str, Any]] = []

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search" and payload is not None:
            captured.append(dict(payload))
            return {"hits": [], "revision": 1}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)
    provider.prefetch("测试一下召回")
    assert len(captured) == 1
    assert captured[0].get("inject") is True


def test_8_session_links_point_back_to_source_session(tmp_path: Path) -> None:
    """非日记笔记要用 Obsidian 双链指向来源会话；会话索引不进检索。"""
    from agentbrain.index import reindex

    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    sid = "local-0123456789abcdef0123456789abcdef"
    store, job, events = _seed_claimed_job(
        cfg, clock, session_id=sid, event_ids=("ev1", "ev2", "ev3", "ev4", "ev5")
    )
    ref = f"hermes:{sid}:ev1"

    summary = validate_and_apply_batch(
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
                    "title": "会话出处可点",
                    "content": "这条结论要能点回它来自哪次对话。",
                    "source_refs": [ref],
                }
            ],
        },
        clock=clock,
    )
    assert summary.applied_count == 1

    note_path = next((cfg.profile_dir / "20_LongTerm" / "Facts").glob("FACT-*-会话出处可点.md"))
    note_text = note_path.read_text(encoding="utf-8")
    stub = cfg.profile_dir / "40_Sessions" / f"S-{sid.split('-', 1)[1][:8]}.md"
    assert stub.exists(), "必须生成会话索引笔记"
    assert "## 来源" in note_text
    assert f"[[{stub.stem}]]" in note_text
    assert sid in note_text  # 完整会话号也留在正文里，方便定位

    stub_text = stub.read_text(encoding="utf-8")
    assert sid in stub_text
    assert f"[[{note_path.stem}]]" in stub_text

    stats = reindex(cfg)
    assert stats.total_notes > 0
    from agentbrain.index import connect_db

    with connect_db(cfg.broker.db_path) as conn:
        hit = conn.execute("SELECT COUNT(*) FROM notes WHERE path LIKE '40_Sessions/%'").fetchone()[0]
    assert hit == 0, "会话索引不能进检索索引"
