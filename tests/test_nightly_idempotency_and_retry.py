"""回归测试：分段重跑幂等(§2)、分段游标即时推进(§3)、保留失败原因(§4)、夜间重试退避(§5)。"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import agentbrain.proactive_worker as proactive_worker_mod
from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_apply import sha256_text, validate_and_apply_batch
from agentbrain.proactive_clock import ManualClock, dt_to_us
from agentbrain.proactive_worker import ModelTransportError, run_worker_once


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


def _create_nightly_job(
    store: ProactiveStore,
    clock: ManualClock,
    *,
    event_contents: list[str],
    day: dt.date = dt.date(2026, 11, 24),
) -> None:
    for idx, text in enumerate(event_contents, start=1):
        store.ingest_event(
            client="hermes",
            session_id="s-night",
            event_id=f"ev-{idx}",
            content=text,
            occurred_at=dt.datetime(day.year, day.month, day.day, 4, idx, tzinfo=dt.timezone.utc),
        )
    clock.set_utc(dt.datetime(day.year, day.month, day.day, 15, 30, tzinfo=dt.timezone.utc))
    store.tick()


# ---------------------------------------------------------------------------
# §2 重跑同一段落必须幂等
# ---------------------------------------------------------------------------


def test_s2_segment_replay_is_idempotent_even_when_reordered(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 23, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    _create_nightly_job(store, clock, event_contents=["事件一：装好插件", "事件二：体检结果正常", "事件三：更新热记忆"])

    job = store.claim_job("w-idem")
    assert job is not None
    events = store.get_job_events(job.job_id)

    hot_file = cfg.profile_dir / "10_Working" / "HOT.md"
    initial_snap = {"10_Working/HOT.md": sha256_text(hot_file.read_text(encoding="utf-8"))}

    ops = [
        {
            "op_type": "longterm_upsert",
            "note_type": "fact",
            "title": "体检结果窦性心律正常",
            "content": "2026-11-24 体检报告显示窦性心律大致正常，暂无需复查。",
            "source_refs": ["ev-2"],
        },
        {
            "op_type": "hot_update",
            "content": "# HOT\n体检正常，插件已就绪。",
            "source_refs": ["ev-3"],
        },
        {
            "op_type": "diary_append",
            "ledger": "public",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "上午验证了官方插件闭环，下午确认体检报告窦性心律正常。",
            "source_refs": ["ev-1"],
        },
        {
            "op_type": "diary_append",
            "ledger": "private",
            "kind": "daily",
            "event_date": "2026-11-24",
            "content": "看到体检报告正常那刻心里悄悄松了口气。",
            "source_refs": ["ev-2"],
        },
    ]

    summary1 = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {"outcome": "operations", "operations": ops},
        segment_index=0,
        clock=clock,
        snapshot_hashes=dict(initial_snap),
    )
    assert summary1.applied_count == len(ops)
    assert summary1.skipped_dedupe_count == 0

    # 验证 op_id 格式为 <idempotency_key>:<segment_index>:<digest>，不含 ":op:" 批内序号
    for rec in summary1.operations:
        oid = str(rec["op_id"])
        assert oid.startswith(f"{job.idempotency_key}:0:")
        assert ":op:" not in oid

    pub_file = cfg.profile_dir / "30_Diary" / "Public" / "2026-11-24.md"
    pri_file = cfg.profile_dir / "30_Diary" / "Private" / "2026-11-24.md"
    fact_files = list((cfg.profile_dir / "20_LongTerm" / "Facts").glob("*.md"))
    assert len(fact_files) == 1
    fact_file = fact_files[0]

    pub_hash_1 = sha256_text(pub_file.read_text(encoding="utf-8"))
    pri_hash_1 = sha256_text(pri_file.read_text(encoding="utf-8"))
    fact_hash_1 = sha256_text(fact_file.read_text(encoding="utf-8"))
    hot_hash_1 = sha256_text(hot_file.read_text(encoding="utf-8"))

    # 模拟 job 重试：同一 segment 重放同一批 payload（且倒序排列、仍带初始 snapshot_hashes）
    summary2 = validate_and_apply_batch(
        cfg,
        store,
        job,
        events,
        {"outcome": "operations", "operations": list(reversed(ops))},
        segment_index=0,
        clock=clock,
        snapshot_hashes=dict(initial_snap),
    )
    assert summary2.applied_count == 0
    assert summary2.skipped_dedupe_count == len(ops)
    assert all(r["status"] == "skipped_dedupe" for r in summary2.operations)

    # 日记文件、长期笔记、HOT.md 内容 hash 完全不变
    assert sha256_text(pub_file.read_text(encoding="utf-8")) == pub_hash_1
    assert sha256_text(pri_file.read_text(encoding="utf-8")) == pri_hash_1
    assert sha256_text(fact_file.read_text(encoding="utf-8")) == fact_hash_1
    assert sha256_text(hot_file.read_text(encoding="utf-8")) == hot_hash_1


# ---------------------------------------------------------------------------
# §3 分段进度「应用成功即推进」
# ---------------------------------------------------------------------------


def test_s3_segment_cursor_advances_immediately_and_retry_skips_completed_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 23, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            nightly_split_enabled=False,
            model_max_context_chars=24000,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    _create_nightly_job(store, clock, event_contents=["分段一证据内容", "分段二证据内容"])

    # 强制构造 2 个分段，确保多段路径清晰可控
    real_seg = proactive_worker_mod.segment_job_prompts_with_snapshots

    def two_segments(config: AppConfig, job: Any, events: list[Any], *, store: ProactiveStore | None = None):
        _, snaps = real_seg(config, job, events, store=store)
        assert len(events) == 2
        return [([events[0]], "PROMPT_SEG_0"), ([events[1]], "PROMPT_SEG_1")], snaps

    monkeypatch.setattr(proactive_worker_mod, "segment_job_prompts_with_snapshots", two_segments)

    model_prompts: list[str] = []

    def model_runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        user_prompt = str(req.get("user_prompt") or "")
        model_prompts.append(user_prompt)
        if "PROMPT_SEG_0" in user_prompt:
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "digest": "第一段完成事实整理",
                    "operations": [
                        {
                            "op_type": "longterm_upsert",
                            "note_type": "fact",
                            "title": "第一段事实",
                            "content": "来自第一段的长期事实。",
                            "source_refs": ["ev-1"],
                        }
                    ],
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
                        "kind": "daily",
                        "event_date": "2026-11-24",
                        "content": "今天顺利完成了两段夜间复盘。",
                        "source_refs": ["ev-2"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "daily",
                        "event_date": "2026-11-24",
                        "content": "夜间分段推进顺滑，不用再反复返工。",
                        "source_refs": ["ev-2"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "kind": "wander",
                        "event_date": "2026-11-24",
                        "content": "漫游思考：把状态切小比事后补救更省心。",
                        "source_refs": [],
                    },
                ],
            },
        }

    real_apply = proactive_worker_mod.validate_and_apply_batch
    seg1_failed_once = {"done": False}

    def fail_on_seg1_once(*args: Any, **kwargs: Any) -> Any:
        seg_idx = kwargs.get("segment_index", 0)
        if seg_idx == 1 and not seg1_failed_once["done"]:
            seg1_failed_once["done"] = True
            raise RuntimeError("simulated seg 1 apply boom")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(proactive_worker_mod, "validate_and_apply_batch", fail_on_seg1_once)

    # 第一次运行：第 1 段应用成功，第 2 段应用抛异常
    res1 = run_worker_once(cfg, store, clock=clock, hermes_runner=model_runner)
    assert res1 is None
    assert len(model_prompts) == 2

    st_after_fail = store.get_status()
    job_id = st_after_fail["jobs"][0]["id"]
    assert st_after_fail["jobs"][0]["status"] == "retry_wait"

    # 游标必须已停在 1（第 1 段已记）
    cursor = store.get_adapter_cursor("worker_segment", f"job_segment_progress:{job_id}")
    assert cursor is not None
    assert cursor["completed_segments"] == 1

    # 推进时钟越过退避等待后重试：不能重跑第 1 段，只调用 1 次模型（仅第 2 段）
    clock.advance(seconds=120)
    model_prompts.clear()
    res2 = run_worker_once(cfg, store, clock=clock, hermes_runner=model_runner)
    assert res2 is not None
    assert len(model_prompts) == 1
    assert "PROMPT_SEG_1" in model_prompts[0]
    assert "PROMPT_SEG_0" not in model_prompts[0]

    st_final = store.get_status()
    assert st_final["jobs"][0]["status"] == "succeeded"


def test_s3_cursor_write_failure_warns_and_does_not_fail_applied_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-cur", event_id=f"c-{i}", content=f"msg {i}")

    real_set_cursor = store.set_adapter_cursor

    def flaky_cursor(adapter_name: str, source_key: str, cursor: dict[str, Any]) -> None:
        if adapter_name == "worker_segment" and int(cursor.get("completed_segments") or 0) > 0:
            raise sqlite3.OperationalError("simulated cursor write locked")
        real_set_cursor(adapter_name, source_key, cursor)

    monkeypatch.setattr(store, "set_adapter_cursor", flaky_cursor)

    def ok_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "longterm_upsert",
                        "note_type": "fact",
                        "title": "游标写失败容错事实",
                        "content": "游标写入失败不应把已应用段落判为失败。",
                        "source_refs": ["c-1"],
                    }
                ],
            },
        }

    with caplog.at_level(logging.WARNING, logger="agentbrain.proactive_worker"):
        res = run_worker_once(cfg, store, clock=clock, hermes_runner=ok_runner)

    assert res is not None
    assert res.applied_count == 1
    assert store.get_status()["jobs"][0]["status"] == "succeeded"
    assert any("worker_segment 游标写入失败" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# §4 保留失败原因（可诊断）
# ---------------------------------------------------------------------------


def test_s4_last_error_preserved_after_retry_success_and_exposed_in_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-err", event_id=f"e-{i}", content=f"turn {i}")

    calls = {"n": 0}
    err_detail = "upstream gateway 502 bad gateway"

    def flaky_model(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelTransportError(err_detail)
        return {"outcome": "no_change", "operations": []}

    monkeypatch.setattr(proactive_worker_mod, "call_proactive_model", flaky_model)

    # 第 1 次 attempt 抛 ModelTransportError
    res1 = run_worker_once(cfg, store, clock=clock)
    assert res1 is None
    assert calls["n"] == 1

    # 推进时钟后第 2 次成功
    clock.advance(seconds=120)
    res2 = run_worker_once(cfg, store, clock=clock)
    assert res2 is not None
    assert calls["n"] == 2

    # 直接查 DB：last_error 仍保留第 1 次错误原文，且 last_error_at_utc 非空
    with sqlite3.connect(cfg.proactive_db_path) as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error, last_error_at_utc FROM jobs WHERE id = ?",
            (res2.job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert int(row[1]) == 2
    assert "ModelTransportError" in str(row[2])
    assert err_detail in str(row[2])
    assert row[3] is not None and str(row[3]).strip() != ""

    # review-status 数据源也能直接看到这单重试过及上次失败原因
    st = store.get_status()
    j_info = st["jobs"][0]
    assert j_info["status"] == "succeeded"
    assert j_info["attempts"] == 2
    assert err_detail in str(j_info["last_error"])
    assert j_info["last_error_at_utc"] == row[3]


# ---------------------------------------------------------------------------
# §5 夜间重试退避（60 / 180 / 300 秒）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job_type", ["nightly", "nightly_work", "nightly_diary"])
def test_s5_nightly_retry_backoff_60_180_300_and_cap(tmp_path: Path, job_type: str) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 15, 30, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path, proactive=ProactiveConfig(enabled=True, max_attempts=5))
    store = ProactiveStore.from_app_config(cfg, clock=clock)

    now_utc = clock.now_utc()
    now_us = dt_to_us(now_utc)
    with store._tx() as conn:
        store._enqueue_nightly_job(
            conn,
            window_id="2026-11-24",
            revision=1,
            since_event_id=0,
            until_event_id=1,
            event_count=1,
            now_utc=now_utc,
            now_us=now_us,
            job_type=job_type,
        )

    expected_delays = [60, 180, 300, 300]
    for idx, expected_sec in enumerate(expected_delays, start=1):
        claimed = store.claim_job(f"w-backoff-{idx}")
        assert claimed is not None
        fail_ts_us = dt_to_us(clock.now_utc())
        new_status = store.ack_job_failure(claimed.job_id, claimed.lease_token, error=f"fail #{idx}")
        assert new_status == "retry_wait"

        with sqlite3.connect(cfg.proactive_db_path) as conn:
            row = conn.execute(
                "SELECT next_retry_at_us FROM jobs WHERE id = ?",
                (claimed.job_id,),
            ).fetchone()
        assert row is not None and row[0] is not None
        actual_delay_sec = (int(row[0]) - fail_ts_us) // 1_000_000
        assert actual_delay_sec == expected_sec

        # 推进时钟到刚过退避点以便下一次认领
        clock.advance(seconds=expected_sec)


def test_s5_incremental_retry_backoff_unchanged(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, base_backoff_seconds=30, max_backoff_seconds=3600, max_attempts=5),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-inc", event_id=f"inc-{i}", content=f"msg {i}")

    for idx, expected_sec in enumerate([30, 60, 120], start=1):
        claimed = store.claim_job(f"w-inc-{idx}")
        assert claimed is not None
        fail_ts_us = dt_to_us(clock.now_utc())
        assert store.ack_job_failure(claimed.job_id, claimed.lease_token, error=f"inc fail #{idx}") == "retry_wait"
        with sqlite3.connect(cfg.proactive_db_path) as conn:
            row = conn.execute("SELECT next_retry_at_us FROM jobs WHERE id = ?", (claimed.job_id,)).fetchone()
        assert row is not None and row[0] is not None
        assert (int(row[0]) - fail_ts_us) // 1_000_000 == expected_sec
        clock.advance(seconds=expected_sec)
