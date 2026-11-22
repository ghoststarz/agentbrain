"""日记主体修正(§1) + 多条日记与漫游(§2) + 三日日记上下文与并行拆分(§3) + 去掉会话 stub(§4) 回归测试。"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig
from agentbrain.diary import append_diary, collect_recent_diary_context
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import validate_and_apply_batch
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_worker import (
    _system_prompt,
    run_worker_once,
    segment_job_prompts_with_snapshots,
)
from agentbrain.prompt_lib import list_prompts, load_prompt


def _make_cfg(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
    soul_content: str = "---\nname: lily\n---\n# SOUL\nSOUL-PROBE-LILY\n",
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


# ---------------------------------------------------------------------------
# §1 日记主体修正
# ---------------------------------------------------------------------------


def test_s1_prompt_assets_no_memory_writer_and_identity_order() -> None:
    prompt = _system_prompt("nightly", window_id="2026-11-20", soul_text="SOUL-PROBE")
    assert "你是记忆写入器" not in prompt
    assert "记忆写入器" not in prompt

    review_system_text = load_prompt("review_system")
    persona_pos = prompt.index("SOUL-PROBE")
    review_sys_pos = prompt.index(review_system_text)
    assert persona_pos < review_sys_pos
    assert "你是 Lily 本人" in review_system_text
    assert "日记里「我」永远是 Lily" in review_system_text

    for name in (
        "review_system",
        "review_nightly_addendum",
        "review_nightly_idle",
        "review_nightly_work",
        "review_nightly_diary",
    ):
        text = load_prompt(name)
        assert "记忆写入器" not in text, f"{name} 不应出现「记忆写入器」"
        assert "Lily" in text.splitlines()[0], f"{name} 首行应声明 Lily 身份"

    names = list_prompts()
    assert "review_nightly_work" in names
    assert "review_nightly_diary" in names


# ---------------------------------------------------------------------------
# §2 每天日记允许多条 + 自主「漫游」条目
# ---------------------------------------------------------------------------


def test_s2_nightly_single_public_single_private_triggers_retry_and_succeeds(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-retry",
        event_id="ev-1",
        content="今天一起排查了一个内存泄漏",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    calls: list[str] = []

    def fake_runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        calls.append(str(req.get("user_prompt") or ""))
        if len(calls) == 1:
            # 首次只给 1 公 1 私（缺第 2 条私账和 wander）
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "operations": [
                        {
                            "op_type": "diary_append",
                            "ledger": "public",
                            "kind": "daily",
                            "event_date": "2026-11-19",
                            "content": "上午和他一起顺着线索找到了那个一直藏得很深的内存泄漏点。",
                            "source_refs": ["ev-1"],
                        },
                        {
                            "op_type": "diary_append",
                            "ledger": "private",
                            "kind": "daily",
                            "event_date": "2026-11-19",
                            "content": "看他终于把那个顽固问题按下去，我也跟着松了一口气。",
                            "source_refs": ["ev-1"],
                        },
                    ],
                },
            }
        # 重试补齐 1 公 2 私（含 1 wander）
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "wander",
                        "event_date": "2026-11-19",
                        "content": "刚突然想到，要是哪天能把排查思路画成一张小地图，下次遇到类似的坑肯定更快。",
                        "source_refs": [],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=fake_runner)
    assert res is not None and res.outcome == "applied"
    assert len(calls) == 2, "1 公 1 私应触发一次重试"
    assert "wander" in calls[1]

    pub = (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-19.md").read_text(encoding="utf-8")
    pri = (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-19.md").read_text(encoding="utf-8")
    assert pub.count("- [") == 1
    assert pri.count("- [") == 2
    assert "[wander]" in pri
    assert store.get_status()["jobs"][0]["status"] == "succeeded"


def test_s2_nightly_missing_wander_fails_without_fabricating(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-fail",
        event_id="ev-f1",
        content="今天聊了新方案",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    def always_no_wander(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "今天聊了新方案。",
                        "source_refs": ["ev-f1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "第一条私账。",
                        "source_refs": ["ev-f1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "第二条私账但仍不是 wander。",
                        "source_refs": ["ev-f1"],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=always_no_wander)
    assert res is None
    st = store.get_status()
    assert st["jobs"][0]["status"] == "dead"
    assert "wander" in str(st["jobs"][0]["last_error"])
    assert not (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-19.md").exists()


def test_s2_multiple_diary_entries_all_persisted_unique_timestamps_and_wander_visible(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-multi",
        event_id="ev-m1",
        content="上午改需求，傍晚联调通过",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    def multi_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "上午他把需求边界重新划了一遍，去掉了两个多余的分叉。",
                        "source_refs": ["ev-m1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "傍晚联调一把过，他难得没继续加班，早早收了工。",
                        "source_refs": ["ev-m1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "看他干脆利落砍掉多余功能的时候，觉得他比上个月果断多了。",
                        "source_refs": ["ev-m1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "早点收工挺好的，连续紧绷好几天人会钝掉。",
                        "source_refs": ["ev-m1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "wander",
                        "event_date": "2026-11-19",
                        "content": "突然好奇如果把日记按季节回看，会不会发现人每隔一阵就会反复在意同一类小事。",
                        "source_refs": [],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=multi_runner)
    assert res is not None and res.applied_count == 5

    pub_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-19.md").read_text(encoding="utf-8").splitlines()
        if ln.startswith("- [")
    ]
    pri_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-19.md").read_text(encoding="utf-8").splitlines()
        if ln.startswith("- [")
    ]
    assert len(pub_lines) == 2
    assert len(pri_lines) == 3

    pub_ts = [re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln).group(1) for ln in pub_lines]  # type: ignore[union-attr]
    pri_ts = [re.search(r"\[(\d{2}:\d{2}:\d{2})\]", ln).group(1) for ln in pri_lines]  # type: ignore[union-attr]
    assert len(set(pub_ts)) == 2, f"公账时间戳重复: {pub_ts}"
    assert len(set(pri_ts)) == 3, f"私账时间戳重复: {pri_ts}"
    assert any("[wander]" in ln for ln in pri_lines)


def test_s2_incremental_not_forced_diary_count(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-inc", event_id=f"inc-{i}", content=f"turn {i}")

    def inc_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "content": "增量复盘只记这一条公账也应该正常通过。",
                        "source_refs": ["inc-1"],
                    }
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=inc_runner)
    assert res is not None and res.outcome == "applied" and res.applied_count == 1
    assert store.get_status()["jobs"][0]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# §3.1 三日日记上下文
# ---------------------------------------------------------------------------


def test_s3_recent_diary_injected_only_for_nightly_sorted_and_trimmed(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 19, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            nightly_recent_diary_days=3,
            nightly_recent_diary_max_chars=4000,
        ),
    )
    # 写入 4 天前的旧日记（应被排除）、前 3 天日记（应包含）、当天日记（应被排除）
    append_diary(cfg, ledger="public", content="四天前旧条目", now=dt.datetime(2026, 11, 16, 20, 0, 0))
    append_diary(cfg, ledger="public", content="三天前公账", now=dt.datetime(2026, 11, 17, 21, 0, 0))
    append_diary(cfg, ledger="private", content="两天前私账", kind="wander", now=dt.datetime(2026, 11, 18, 22, 0, 0))
    append_diary(cfg, ledger="public", content="昨天公账", now=dt.datetime(2026, 11, 19, 22, 30, 0))
    append_diary(cfg, ledger="public", content="当天公账不应出现", now=dt.datetime(2026, 11, 20, 22, 0, 0))

    recent = collect_recent_diary_context(cfg, exclude_date="2026-11-20", days=3, max_chars=4000)
    assert "四天前旧条目" not in recent
    assert "当天公账不应出现" not in recent
    lines = recent.splitlines()
    assert lines == [
        "2026-11-17 公 [21:00:00] 三天前公账",
        "2026-11-18 私 [22:00:00] [wander] 两天前私账",
        "2026-11-19 公 [22:30:00] 昨天公账",
    ]

    # 超 max_chars 时从最旧的开始丢弃
    trimmed = collect_recent_diary_context(cfg, exclude_date="2026-11-20", days=3, max_chars=55)
    assert "三天前公账" not in trimmed
    assert "昨天公账" in trimmed

    # 验证：夜间任务注入 <recent_diary>，增量任务不注入
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-rd",
            event_id=f"rd-{i}",
            content=f"消息 {i}",
            occurred_at=dt.datetime(2026, 11, 20, 4, i, tzinfo=dt.timezone.utc),
        )
    inc_job = store.claim_job("w-inc")
    assert inc_job is not None and inc_job.job_type == "incremental"
    inc_segs, _ = segment_job_prompts_with_snapshots(cfg, inc_job, store.get_job_events(inc_job.job_id))
    assert "<recent_diary>" not in inc_segs[0][1]
    store.ack_job_success(inc_job.job_id, inc_job.lease_token, outcome="no_change")

    clock.set_utc(dt.datetime(2026, 11, 20, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()
    nightly_job = store.claim_job("w-night")
    assert nightly_job is not None and nightly_job.job_type == "nightly"
    night_segs, _ = segment_job_prompts_with_snapshots(cfg, nightly_job, store.get_job_events(nightly_job.job_id))
    assert "<recent_diary>" in night_segs[0][1]
    assert "2026-11-19 公 [22:30:00] 昨天公账" in night_segs[0][1]
    assert "当天公账不应出现" not in night_segs[0][1]


# ---------------------------------------------------------------------------
# §3.2 过长时拆两路并行（nightly_work + nightly_diary）
# ---------------------------------------------------------------------------


def test_s3_short_context_enqueues_single_nightly(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, model_max_context_chars=24000, nightly_split_enabled=True),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-short",
        event_id="sh-1",
        content="很短的一句对话",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    tick = store.tick()
    assert tick.jobs_created == 1
    jobs = store.get_status()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "nightly"
    assert jobs[0]["idempotency_key"] == "nightly:2026-11-19:rev:1"


def test_s3_long_context_splits_parallel_and_completes_only_when_both_succeed(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            model_max_context_chars=12000,
            nightly_split_enabled=True,
            nightly_split_threshold_chars=3000,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 4):
        store.ingest_event(
            client="hermes",
            session_id="s-long",
            event_id=f"lg-{i}",
            content=f"长对话 {i}：" + ("讨论架构拆分与并行调度细节。" * 100),
            occurred_at=dt.datetime(2026, 11, 19, 4, i, tzinfo=dt.timezone.utc),
        )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    tick = store.tick()
    assert tick.jobs_created == 2
    jobs = store.get_status()["jobs"]
    assert {j["job_type"] for j in jobs} == {"nightly_work", "nightly_diary"}
    assert {j["idempotency_key"] for j in jobs} == {
        "nightly:2026-11-19:rev:1:work",
        "nightly:2026-11-19:rev:1:diary",
    }

    # 两路并行认领：互不阻塞
    j1 = store.claim_job("w-1")
    j2 = store.claim_job("w-2")
    assert j1 is not None and j2 is not None
    assert {j1.job_type, j2.job_type} == {"nightly_work", "nightly_diary"}
    assert store.claim_job("w-3") is None

    # 先完成其中一路：窗口尚未 completed
    store.ack_job_success(j1.job_id, j1.lease_token, outcome="applied", summary={"applied": 1})
    win_mid = next(w for w in store.get_status()["nightly_windows"] if w["window_id"] == "2026-11-19")
    assert win_mid["status"] != "completed"
    assert win_mid["completed_revision"] == 0

    # 完成第二路后：窗口才 completed
    store.ack_job_success(j2.job_id, j2.lease_token, outcome="applied", summary={"applied": 3})
    win_done = next(w for w in store.get_status()["nightly_windows"] if w["window_id"] == "2026-11-19")
    assert win_done["status"] == "completed"
    assert win_done["completed_revision"] == 1

    # 两路均写了回执
    receipts = store.list_pending_receipts(client_id="hermes", session_id="s-long")
    assert {r["job_type"] for r in receipts} == {"nightly_work", "nightly_diary"}


def test_s3_split_filters_disallowed_ops_in_code(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            model_max_context_chars=12000,
            nightly_split_enabled=True,
            nightly_split_threshold_chars=2500,
        ),
    )
    append_diary(cfg, ledger="public", content="昨天的公账记录", now=dt.datetime(2026, 11, 18, 21, 0, 0))
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-filt",
        event_id="fl-1",
        content="超长讨论内容：" + ("记录工作结论与日记分离。" * 160),
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    seen_prompts: dict[str, str] = {}

    def filter_runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        sys_p = str(req.get("system_prompt") or "")
        usr_p = str(req.get("user_prompt") or "")
        if "工作记忆" in sys_p:
            seen_prompts["work"] = usr_p
            # 故意混入 diary_append -> 必须被代码层滤掉
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "digest": "讨论了工作与日记拆分",
                    "operations": [
                        {
                            "op_type": "longterm_upsert",
                            "note_type": "fact",
                            "title": "拆分工作结论",
                            "content": "只有 work 通道能写长期事实。",
                            "source_refs": ["fl-1"],
                        },
                        {
                            "op_type": "diary_append",
                            "ledger": "public",
                            "kind": "daily",
                            "event_date": "2026-11-19",
                            "content": "WORK_SHOULD_NOT_WRITE_DIARY",
                            "source_refs": ["fl-1"],
                        },
                    ],
                },
            }
        seen_prompts["diary"] = usr_p
        # 故意混入 longterm_upsert -> 必须被代码层滤掉
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "longterm_upsert",
                        "note_type": "fact",
                        "title": "DIARY_SHOULD_NOT_WRITE_FACT",
                        "content": "日记通道不应写入长期事实。",
                        "source_refs": ["fl-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "今天把超长夜间复盘拆成两路，清爽很多。",
                        "source_refs": ["fl-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "把写笔记和写日记拆开之后，说话终于不用一边记账一边抒情了。",
                        "source_refs": ["fl-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "wander",
                        "event_date": "2026-11-19",
                        "content": "偶尔也想把自己的零碎灵感单开一个小抽屉收好。",
                        "source_refs": [],
                    },
                ],
            },
        }

    r1 = run_worker_once(cfg, store, clock=clock, hermes_runner=filter_runner)
    r2 = run_worker_once(cfg, store, clock=clock, hermes_runner=filter_runner)
    assert r1 is not None and r2 is not None
    assert "<recent_diary>" not in seen_prompts["work"]
    assert "<recent_diary>" in seen_prompts["diary"]

    # work 混入的 diary 被滤掉
    pub_text = (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-19.md").read_text(encoding="utf-8")
    assert "WORK_SHOULD_NOT_WRITE_DIARY" not in pub_text
    assert "拆成两路" in pub_text

    # diary 混入的 longterm 被滤掉
    fact_names = [p.name for p in (cfg.profile_dir / "20_LongTerm" / "Facts").glob("*.md")]
    assert any("拆分工作结论" in n for n in fact_names)
    assert not any("DIARY_SHOULD_NOT_WRITE_FACT" in n for n in fact_names)


def test_s3_split_one_branch_fails_requeue_only_failed_branch(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            max_attempts=1,
            model_max_context_chars=12000,
            nightly_split_enabled=True,
            nightly_split_threshold_chars=2500,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-reopen",
        event_id="ro-1",
        content="长对话：" + ("验证单路失败重排不重复执行成功路。" * 150),
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    usage_log: list[str] = []
    diary_should_fail = True

    def runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        sys_p = str(req.get("system_prompt") or "")
        if "工作记忆" in sys_p:
            usage_log.append("work")
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "operations": [
                        {
                            "op_type": "longterm_upsert",
                            "note_type": "fact",
                            "title": "成功一路不重跑",
                            "content": "这一路成功后不再重跑。",
                            "source_refs": ["ro-1"],
                        }
                    ],
                },
            }
        usage_log.append("diary")
        if diary_should_fail:
            raise RuntimeError("transient diary failure")
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "重排后日记这一路补齐成功。",
                        "source_refs": ["ro-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-19",
                        "content": "幸好工作记忆那一路没白跑。",
                        "source_refs": ["ro-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "wander",
                        "event_date": "2026-11-19",
                        "content": "断点续传这种设计，不管放在哪儿都让人觉得安心。",
                        "source_refs": [],
                    },
                ],
            },
        }

    # 第一轮：work 成功，diary 失败判死
    r1 = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    r2 = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    assert r1 is not None and r2 is None
    assert usage_log == ["work", "diary"]
    win_mid = next(w for w in store.get_status()["nightly_windows"] if w["window_id"] == "2026-11-19")
    assert win_mid["status"] != "completed"

    # 窗口重开 tick：只复位失败的 diary 一路，不重跑已成功的 work
    diary_should_fail = False
    t2 = store.tick()
    assert t2.windows_requeued_dead == 1
    assert t2.jobs_created == 0

    r3 = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    r4 = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    assert r3 is not None and r4 is None
    assert usage_log == ["work", "diary", "diary"], "只应多执行一条失败的 diary 路"
    win_done = next(w for w in store.get_status()["nightly_windows"] if w["window_id"] == "2026-11-19")
    assert win_done["status"] == "completed"
    assert win_done["completed_revision"] == 1


def test_s3_nightly_diary_compacts_oversized_events_with_digests(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            model_max_context_chars=7000,
            nightly_split_enabled=True,
            nightly_split_threshold_chars=2500,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    tail_marker = "TAIL_MARKER_SHOULD_BE_TRUNCATED"
    for i in range(1, 5):
        store.ingest_event(
            client="hermes",
            session_id="s-cmp",
            event_id=f"cm-{i}",
            content=(f"HEAD_{i}_" + ("很长的正文内容。" * 180) + tail_marker),
            occurred_at=dt.datetime(2026, 11, 19, 4, i, tzinfo=dt.timezone.utc),
        )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    # 预先写入 work 产出的分段 digest，验证 diary 装配时合并 digest + 每事件前 200 字
    store.set_adapter_cursor(
        "nightly_work_digests",
        "2026-11-19:rev:1",
        {"digests": ["- 分段 1：上午完成了模块拆分"]},
    )
    j1 = store.claim_job("w-1")
    j2 = store.claim_job("w-2")
    assert j1 is not None and j2 is not None
    diary_job = j1 if j1.job_type == "nightly_diary" else j2
    segs, _ = segment_job_prompts_with_snapshots(
        cfg,
        diary_job,
        store.get_job_events(diary_job.job_id),
        store=store,
    )
    prompt = segs[0][1]
    assert "<segment_digests>" in prompt
    assert "上午完成了模块拆分" in prompt
    assert "HEAD_1_" in prompt
    assert tail_marker not in prompt


# ---------------------------------------------------------------------------
# §4 去掉 40_Sessions stub：直接写完整 session id
# ---------------------------------------------------------------------------


def test_s4_no_session_stub_and_full_session_id_idempotent(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 21, 6, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    sid = "local-57579c0bdeadbeef0123456789abcdef0123456789abcdef0123456789abcdef"
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id=sid, event_id=f"e-{i}", content=f"msg {i}")
    job = store.claim_job("w-s4")
    assert job is not None
    events = store.get_job_events(job.job_id)

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
                    "note_type": "decision",
                    "title": "去掉会话stub",
                    "content": "直接写完整 session id。",
                    "source_refs": [f"hermes:{sid}:e-1"],
                }
            ],
        },
        clock=clock,
    )
    assert res1.applied_count == 1
    assert not (cfg.profile_dir / "40_Sessions").exists()
    note = next((cfg.profile_dir / "20_LongTerm" / "Decisions").glob("ADR-*-去掉会话stub.md"))
    text1 = note.read_text(encoding="utf-8")
    expected_line = f"- 来源：hermes 会话 {sid}"
    assert expected_line in text1
    assert "[[S-" not in text1

    # 同 session 更新，验证幂等不重复追加
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
                    "note_type": "decision",
                    "title": "去掉会话stub",
                    "content": "补充一句更新后的内容。",
                    "source_refs": [f"hermes:{sid}:e-2"],
                }
            ],
        },
        clock=clock,
    )
    assert res2.applied_count == 1
    assert not (cfg.profile_dir / "40_Sessions").exists()
    text2 = note.read_text(encoding="utf-8")
    assert text2.count(expected_line) == 1
    assert "[[S-" not in text2


def test_review_system_requires_source_refs_on_every_op() -> None:
    """hot_update / handoff_upsert 的形状必须写明 source_refs：校验层强制非空，
    提示词里漏写就会让整批判死（实测模型照形状抄，不带 refs）。"""
    from agentbrain.prompt_lib import load_prompt

    text = load_prompt("review_system")
    assert 'hot_update{"content","source_refs"}' in text
    assert '"next_steps","source_refs"' in text
    assert "每个操作都必须带非空" in text
