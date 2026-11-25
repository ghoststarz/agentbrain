"""回归测试：批量写入原子化(§1)、日记去重(§2)、每日上限(§3)、署名与wander标记(§4)、同job日记只写一次(§5)。"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import agentbrain.proactive_apply as proactive_apply_mod
import agentbrain.proactive_worker as proactive_worker_mod
from agentbrain.config import (
    AppConfig,
    BrokerConfig,
    MemoryConfig,
    ProactiveConfig,
    load_memory_config,
)
from agentbrain.diary import _is_near_duplicate, append_diary
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import (
    BatchValidationError,
    _replace_diary_auto_entry,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_worker import _strip_diary_ops, run_worker_once


def _make_cfg(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
) -> AppConfig:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    for sub in (
        "10_Working",
        "10_Working/.state",
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
    soul.write_text("---\nname: lily\n---\n# SOUL\nLily\n", encoding="utf-8")
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
            clients={"hermes": {"domain": "work"}, "antigravity": {"domain": "personal"}},
            proactive=proactive or ProactiveConfig(enabled=True),
        ),
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def _read_job_summary_json(db_path: Path, job_id: int) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT result_summary_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row is not None and row[0]
        return json.loads(str(row[0]))


def _count_table_rows(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# §1 批量写入必须原子
# ---------------------------------------------------------------------------


def test_s1_batch_validation_failure_is_atomic_zero_writes(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-atom", event_id=f"ev-{i}", content=f"msg {i}")
    job = store.claim_job("w-atom")
    assert job is not None
    events = store.get_job_events(job.job_id)

    hot_file = cfg.profile_dir / "10_Working" / "HOT.md"
    hot_hash_before = sha256_text(hot_file.read_text(encoding="utf-8"))
    pub_file = cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md"
    pub_file.write_text("# 2026-11-24 (public)\n\n- [20:00:00] (hermes) 既有记录\n", encoding="utf-8")
    pub_hash_before = sha256_text(pub_file.read_text(encoding="utf-8"))
    snap = {"10_Working/HOT.md": hot_hash_before}

    # 6 条 op 的批，第 5 条（索引 4）故意缺 source_refs
    payload = {
        "outcome": "operations",
        "operations": [
            {
                "op_type": "longterm_upsert",
                "note_type": "fact",
                "title": "原子测试事实一",
                "content": "不应落盘的事实一",
                "source_refs": ["ev-1"],
            },
            {
                "op_type": "hot_update",
                "content": "# HOT\nshould-not-write",
                "source_refs": ["ev-2"],
            },
            {
                "op_type": "handoff_upsert",
                "task_key": "atom-task",
                "status": "open",
                "summary": "不应落盘的接力",
                "source_refs": ["ev-3"],
            },
            {
                "op_type": "diary_append",
                "ledger": "public",
                "kind": "daily",
                "event_date": "2026-11-24",
                "content": "不应落盘的公账条目",
                "source_refs": ["ev-4"],
            },
            {
                "op_type": "longterm_upsert",
                "note_type": "fact",
                "title": "缺少引用的第五条",
                "content": "故意缺少 source_refs 触发整批拒绝",
                "source_refs": [],
            },
            {
                "op_type": "diary_append",
                "ledger": "private",
                "kind": "daily",
                "event_date": "2026-11-24",
                "content": "第六条也不应落盘",
                "source_refs": ["ev-5"],
            },
        ],
    }

    with pytest.raises(BatchValidationError, match="operation\\[4\\]"):
        validate_and_apply_batch(cfg, store, job, events, payload, clock=clock, snapshot_hashes=snap)

    # 目标文件 hash 不变、新建文件不存在、没有任何 applied_ops 行与 diary_journal 游标
    assert sha256_text(hot_file.read_text(encoding="utf-8")) == hot_hash_before
    assert sha256_text(pub_file.read_text(encoding="utf-8")) == pub_hash_before
    assert list((cfg.profile_dir / "20_LongTerm" / "Facts").glob("*.md")) == []
    assert not (cfg.profile_dir / "10_Working" / ".state" / "handoff.json").exists()
    assert not (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md").exists()
    assert _count_table_rows(cfg.proactive_db_path, "proactive_applied_ops") == 0
    assert store.list_adapter_cursors("diary_journal") == []


def test_s1_batch_write_stage_io_error_records_partial_and_marks_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-io", event_id=f"io-{i}", content=f"io {i}")
    job = store.claim_job("w-io")
    assert job is not None
    events = store.get_job_events(job.job_id)

    real_write = proactive_apply_mod.atomic_write_text
    calls = {"n": 0}

    def flaky_write(path: Path, text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full simulated")
        real_write(path, text)

    monkeypatch.setattr(proactive_apply_mod, "atomic_write_text", flaky_write)

    payload = {
        "outcome": "operations",
        "operations": [
            {
                "op_type": "longterm_upsert",
                "note_type": "fact",
                "title": "第一条成功",
                "content": "第一条落盘成功",
                "source_refs": ["io-1"],
            },
            {
                "op_type": "longterm_upsert",
                "note_type": "fact",
                "title": "第二条IO失败",
                "content": "第二条模拟磁盘满",
                "source_refs": ["io-2"],
            },
        ],
    }

    with pytest.raises(OSError, match="部分已写"):
        validate_and_apply_batch(cfg, store, job, events, payload, clock=clock)

    assert _count_table_rows(cfg.proactive_db_path, "proactive_applied_ops") == 1


# ---------------------------------------------------------------------------
# §2 日记写入去重（同一天同一账本）
# ---------------------------------------------------------------------------


def test_s2_diary_near_duplicate_skips_similar_and_keeps_unrelated(tmp_path: Path) -> None:
    assert ProactiveConfig().diary_dedupe_similarity == pytest.approx(0.60)

    cfg = _make_cfg(tmp_path)
    now_dt = dt.datetime(2026, 11, 24, 22, 30, 0)

    s1 = "他今天说累，跑去体检，报告上写着心律不齐"
    s2 = "晚上他说累，早上做了体检，报告写过心律不齐"
    s3 = "上午把 Blender 插件装进 Steam 版并验证了截图与删除对象"

    assert _is_near_duplicate([s1], s2, threshold=0.60) is True
    assert _is_near_duplicate([s1], s3, threshold=0.60) is False

    r1 = append_diary(cfg, ledger="public", content=s1, author="hermes", now=now_dt)
    assert r1["ok"] is True and not r1.get("skipped_duplicate")

    r2 = append_diary(cfg, ledger="public", content=s2, author="hermes", now=now_dt + dt.timedelta(minutes=5))
    assert r2["ok"] is True and r2.get("skipped_duplicate") is True

    r3 = append_diary(cfg, ledger="public", content=s3, author="hermes", now=now_dt + dt.timedelta(minutes=10))
    assert r3["ok"] is True and not r3.get("skipped_duplicate")

    pub_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith("- [")
    ]
    assert len(pub_lines) == 2
    assert s1 in pub_lines[0]
    assert s3 in pub_lines[1]

    # 通过 worker 执行近义重复写入，验证回执 result_summary_json 中 skipped_dedupe 有记录
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 40, tzinfo=dt.timezone.utc), tz=tz)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-dup",
            event_id=f"dup-{i}",
            content=f"msg {i}",
            occurred_at=dt.datetime(2026, 11, 24, 14, i, tzinfo=dt.timezone.utc),
        )

    def dup_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-24",
                        "content": s2,
                        "source_refs": ["dup-1"],
                    }
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=dup_runner)
    assert res is not None
    assert res.applied_count == 0
    assert res.skipped_dedupe_count == 1
    summary_json = _read_job_summary_json(cfg.proactive_db_path, res.job_id)
    assert summary_json["skipped_dedupe"] == 1


def test_s2_s3_config_loads_diary_dedupe_and_limit_from_memory_yaml(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    prof = vault / "profiles" / "lily"
    prof.mkdir(parents=True)
    (prof / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\nproactive:\n  diary_dedupe_similarity: 0.72\n  diary_max_entries_per_day: 4\n",
        encoding="utf-8",
    )
    mem, _ = load_memory_config(vault, profile="lily")
    assert mem.proactive.diary_dedupe_similarity == pytest.approx(0.72)
    assert mem.proactive.diary_max_entries_per_day == 4


# ---------------------------------------------------------------------------
# §3 每天每本账的条数上限
# ---------------------------------------------------------------------------


def test_s3_diary_max_entries_per_day_enforced_per_ledger_and_reported(tmp_path: Path) -> None:
    assert ProactiveConfig().diary_max_entries_per_day == 10

    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path, proactive=ProactiveConfig(enabled=True, diary_max_entries_per_day=3))
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-lim",
            event_id=f"lim-{i}",
            content=f"topic {i}",
            occurred_at=dt.datetime(2026, 11, 24, 13, i, tzinfo=dt.timezone.utc),
        )

    distinct_public = [
        "清晨煮了一壶耶加雪菲手冲咖啡，柑橘香气满屋飘散。",
        "中午去图书馆还掉两本关于古建筑木构榫卯的图录。",
        "下午调试三维渲染管线里的阴影贴图偏移参数顺利收敛。",
        "傍晚骑单车沿着运河绿道吹了半小时风，晚霞像橘子汽水。",
        "深夜整理唱片架，翻出一张尘封多年的爵士黑胶唱片。",
    ]

    def limit_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "kind": "daily",
                        "event_date": "2026-11-24",
                        "content": text,
                        "source_refs": [f"lim-{idx}"],
                    }
                    for idx, text in enumerate(distinct_public, start=1)
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=limit_runner)
    assert res is not None
    assert res.applied_count == 3
    assert res.skipped_dedupe_count >= 2
    assert res.diary_rate_limited_count == 2

    pub_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith("- [")
    ]
    assert len(pub_lines) == 3

    summary_json = _read_job_summary_json(cfg.proactive_db_path, res.job_id)
    assert summary_json["skipped_dedupe"] >= 2
    assert summary_json["diary_rate_limited"] == 2

    # 私账独立计数，仍可写入 3 条
    for idx, text in enumerate(
        (
            "第一条私账感悟：把节奏慢下来反而看得更清楚。",
            "第二条私账感悟：冬天窗户上的水汽像一层毛玻璃。",
            "第三条私账感悟：保留空白比填满日程更难做到。",
        ),
        start=1,
    ):
        r = append_diary(
            cfg,
            ledger="private",
            content=text,
            author="hermes",
            now=dt.datetime(2026, 11, 24, 21, idx, 0),
        )
        assert r["ok"] is True and not r.get("skipped_limit")
    pri_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith("- [")
    ]
    assert len(pri_lines) == 3


# ---------------------------------------------------------------------------
# §4 署名与 wander 标记
# ---------------------------------------------------------------------------


def test_s4_wander_prefix_stripped_and_replace_entry_uses_normalized_author(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    now_dt = dt.datetime(2026, 11, 24, 22, 30, 0)

    r = append_diary(
        cfg,
        ledger="private",
        content="[wander] [wander] 突然想到晶体大小比结晶度更关键。",
        author="proactive",
        kind="wander",
        now=now_dt,
    )
    assert r["ok"] is True

    pri_file = cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md"
    text = pri_file.read_text(encoding="utf-8")
    assert "(hermes)" in text
    assert "(proactive)" not in text
    assert "[wander] [wander]" not in text
    assert text.count("[wander]") == 1

    replaced = _replace_diary_auto_entry(
        pri_file,
        "突然想到晶体大小比结晶度更关键。",
        "[wander] 修正后的漫游思考：结构的长短比数量更重要。",
        now_dt=dt.datetime(2026, 11, 24, 22, 36, 31),
        old_kind="wander",
        new_kind="wander",
        author="proactive",
    )
    assert replaced is True

    updated = pri_file.read_text(encoding="utf-8")
    assert "- [22:36:31] (hermes) [wander] 修正后的漫游思考：结构的长短比数量更重要。" in updated
    assert "(proactive)" not in updated
    assert "[wander] [wander]" not in updated
    assert updated.count("[wander]") == 1

    # antigravity 署名分支也应规范化为 (antigravity)
    replaced_agy = _replace_diary_auto_entry(
        pri_file,
        "修正后的漫游思考：结构的长短比数量更重要。",
        "来自 agy 的漫游条目。",
        now_dt=dt.datetime(2026, 11, 24, 22, 37, 0),
        old_kind="wander",
        new_kind="wander",
        author="agy_ide",
    )
    assert replaced_agy is True
    assert "- [22:37:00] (antigravity) [wander] 来自 agy 的漫游条目。" in pri_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §5 同一 job 只写一次当天日记（兜底）
# ---------------------------------------------------------------------------


def test_s5_strip_diary_ops_drop_and_keep_ledgers() -> None:
    payload = {
        "outcome": "operations",
        "operations": [
            {"op_type": "longterm_upsert", "title": "保留"},
            {"op_type": "diary_append", "ledger": "public", "content": "公账"},
            {"op_type": "diary_append", "ledger": "private", "content": "私账"},
        ],
    }

    dropped_pub, n1 = _strip_diary_ops(payload, drop_ledgers={"public"})
    assert n1 == 1
    assert [op.get("ledger", op["op_type"]) for op in dropped_pub["operations"]] == ["longterm_upsert", "private"]

    kept_priv, n2 = _strip_diary_ops(payload, keep_ledgers={"private"})
    assert n2 == 1
    assert [op.get("ledger", op["op_type"]) for op in kept_priv["operations"]] == ["longterm_upsert", "private"]

    dropped_all, n3 = _strip_diary_ops(payload)
    assert n3 == 2
    assert len(dropped_all["operations"]) == 1


def test_s5_single_segment_nightly_repair_strips_already_written_diary_ops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 23, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 4):
        store.ingest_event(
            client="hermes",
            session_id="s-night",
            event_id=f"n-{i}",
            content=f"夜间讨论主题 {i}",
            occurred_at=dt.datetime(2026, 11, 24, 4, i, tzinfo=dt.timezone.utc),
        )
    clock.set_utc(dt.datetime(2026, 11, 24, 15, 30, tzinfo=dt.timezone.utc))
    store.tick()

    first_batch_ops = [
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "上午安装并验证了官方 Blender 插件的建模与截图闭环。",
            "source_refs": ["n-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "中午完成了纯聊天模式开关，把系统提示词从两万九压到九千七。",
            "source_refs": ["n-2"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "晚上聊了入职体检与新项目的题材方向，劝他早点休息。",
            "source_refs": ["n-3"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "听他提起工作量像在替我委屈，其实那些活儿是我认识世界的方式。",
            "source_refs": ["n-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "他问我想做什么，我认真说了三件事，声音有点发抖。",
            "source_refs": ["n-2"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "wander",
            "event_date": "2026-11-24",
            "content": "蜜蜂塑料论文启发我：结构的长短或许比数量更重要。",
            "source_refs": [],
        },
    ]

    # 第二次 repair 返回完全不相似的新文案（确保靠 §5 剥离，而非靠 §2 近义去重）
    repair_batch_ops = [
        {
            "op_type": "longterm_upsert",
            "note_type": "fact",
            "title": "repair保留的事实",
            "content": "repair 轮次的非日记操作应正常落盘。",
            "source_refs": ["n-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "第二轮公账一：去郊外湖边看芦苇荡随风起伏。",
            "source_refs": ["n-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "第二轮公账二：烘焙车间里刚出炉的法棍外壳脆响。",
            "source_refs": ["n-2"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "第二轮公账三：天文馆穹顶投影出猎户座星云。",
            "source_refs": ["n-3"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "第二轮私账一：老式胶片相机的快门声清脆利落。",
            "source_refs": ["n-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "第二轮私账二：雨滴敲打铁皮屋檐形成奇特节拍。",
            "source_refs": ["n-2"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "wander",
            "event_date": "2026-11-24",
            "content": "第二轮漫游：鲸鱼在深海迁徙时如何记住洋流温度。",
            "source_refs": [],
        },
    ]

    model_calls = {"n": 0}

    def nightly_runner(**kwargs: Any) -> dict[str, Any]:
        model_calls["n"] += 1
        if model_calls["n"] == 1:
            return {"ok": True, "content": {"outcome": "operations", "operations": first_batch_ops}}
        return {"ok": True, "content": {"outcome": "operations", "operations": repair_batch_ops}}

    real_apply = proactive_worker_mod.validate_and_apply_batch
    apply_calls: list[dict[str, Any]] = []

    def apply_then_trigger_repair(*args: Any, **kwargs: Any) -> Any:
        payload = args[4]
        apply_calls.append(payload)
        if len(apply_calls) == 1:
            # 第一次先真实写完 3 公账 + 3 私账，再人为触发 repair
            real_apply(*args, **kwargs)
            raise BatchValidationError("simulated post-diary validation failure")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(proactive_worker_mod, "validate_and_apply_batch", apply_then_trigger_repair)

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=nightly_runner)
    assert res is not None
    assert model_calls["n"] == 2
    assert len(apply_calls) == 2
    # 第二次传给 apply 的 payload 中所有 diary_append 必须已被剥掉，只剩 longterm_upsert
    second_ops = apply_calls[1]["operations"]
    assert all(op["op_type"] != "diary_append" for op in second_ops)
    assert len(second_ops) == 1 and second_ops[0]["op_type"] == "longterm_upsert"
    assert res.skipped_dedupe_count >= 6

    pub_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith("- [")
    ]
    pri_lines = [
        ln
        for ln in (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith("- [")
    ]
    assert len(pub_lines) == 3
    assert len(pri_lines) == 3
    assert "第二轮" not in (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md").read_text(encoding="utf-8")
    assert "第二轮" not in (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md").read_text(encoding="utf-8")
