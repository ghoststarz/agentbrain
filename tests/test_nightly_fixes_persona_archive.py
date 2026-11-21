"""夜间复盘链路修复 + 人格拆分 + 14天归档 + 命名规范 回归测试。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agentbrain.archive import apply_archive, plan_archive
from agentbrain.config import AppConfig, BrokerConfig, MemoryConfig, ProactiveConfig, load_memory_config
from agentbrain.index import connect_db, reindex, touch_notes_access
from agentbrain.proactive import (
    EVENT_KIND_HUMAN_TURN,
    ProactiveStore,
    StoredEvent,
)
from agentbrain.proactive_apply import normalize_note_title, validate_and_apply_batch
from agentbrain.proactive_clock import ManualClock, dt_to_us
from agentbrain.proactive_runner import ProactiveRunner
from agentbrain.proactive_worker import (
    ContextBudgetExceededError,
    _system_prompt,
    run_worker_once,
    segment_job_prompts_with_snapshots,
)
from agentbrain.prompt_lib import PromptAssetError, load_soul
from agentbrain.retrieve import search_notes


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


def _load_module(name: str, rel_path: str) -> ModuleType:
    root = Path(__file__).resolve().parent.parent
    full = root / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- 需求 1：单条事件超长确定性切分，不 raise ---

def test_req1_oversized_event_splits_deterministically_without_raising(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 20, 1, 0, tzinfo=dt.timezone.utc))
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            model_max_context_chars=2400,
            max_payload_chars=16000,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    body = "".join(f"[{i:04d}:abcdef]" for i in range(250))  # ~3250 chars
    store.ingest_event(
        client="hermes",
        session_id="s-big",
        event_id="big-ev-1",
        content=body,
        occurred_at=dt.datetime(2026, 11, 19, 10, 0, tzinfo=dt.timezone.utc),
    )
    for i in range(2, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-big",
            event_id=f"ev-{i}",
            content=f"short-{i}",
            occurred_at=dt.datetime(2026, 11, 19, 10, i, tzinfo=dt.timezone.utc),
        )
    job = store.claim_job("w-req1")
    assert job is not None
    events = store.get_job_events(job.job_id)

    segs1, _ = segment_job_prompts_with_snapshots(cfg, job, events)
    segs2, _ = segment_job_prompts_with_snapshots(cfg, job, events)
    assert [p for _, p in segs1] == [p for _, p in segs2]
    assert len(segs1) >= 2

    part_prompts = [p for _, p in segs1 if "of event_id=big-ev-1]" in p]
    assert len(part_prompts) >= 2
    n_parts = len(part_prompts)
    for idx, p in enumerate(part_prompts, start=1):
        assert f"[part {idx}/{n_parts} of event_id=big-ev-1]" in p

    # 所有原始片段内容均按顺序无损出现
    combined = "\n".join(part_prompts)
    pos = -1
    for marker in ("[0000:abcdef]", "[0125:abcdef]", "[0249:abcdef]"):
        nxt = combined.find(marker)
        assert nxt > pos
        pos = nxt

    # event_budget < 256 仍保留 raise
    cfg_too_small = _make_cfg(
        tmp_path / "too_small",
        proactive=ProactiveConfig(enabled=True, model_max_context_chars=600),
    )
    with pytest.raises(ContextBudgetExceededError):
        segment_job_prompts_with_snapshots(cfg_too_small, job, events)


# --- 需求 2：Hermes 插件系统通知识别与超长护栏 ---

def test_req2_hermes_plugin_classifies_system_notice_and_guards_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _load_module(
        "agentbrain_plugin_req2",
        "integrations/hermes/agentbrain/__init__.py",
    )
    assert (
        plugin.classify_user_turn("User: [IMPORTANT: You are running as a scheduled cron job ...]")
        == "system_notice"
    )
    assert plugin.classify_user_turn("User: 今天帮我看下代码\nAssistant: 好") == "user"

    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    pdir.mkdir(parents=True)
    (pdir / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\nproactive:\n  enabled: true\n  event_capture: true\n  max_payload_chars: 16000\n",
        encoding="utf-8",
    )
    broker_yaml = tmp_path / "broker.yaml"
    broker_yaml.write_text(f"port: 18799\nvault_root: {vault}\ntoken: tok-test\n", encoding="utf-8")
    monkeypatch.setenv("AGENTBRAIN_BROKER_CONFIG", str(broker_yaml))

    hermes_home = tmp_path / "hermes" / "lily"
    hermes_home.mkdir(parents=True)
    provider = plugin.ObsidianBrokerMemoryProvider()
    provider.initialize("sess-req2", hermes_home=str(hermes_home))

    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(provider, "_enqueue_outbox", lambda ev: enqueued.append(ev))
    monkeypatch.setattr(provider, "_flush_outbox", lambda: None)

    provider.sync_turn(
        "[IMPORTANT: You are running as a scheduled cron job to check status]",
        "Checked.",
        session_id="sess-req2",
    )
    provider.sync_turn(
        "今天帮我看下代码",
        "好",
        session_id="sess-req2",
    )
    # 超长系统通知（>16000）应被跳过，不入库
    provider.sync_turn(
        "[IMPORTANT: You are running as a scheduled cron job] " + ("Z" * 17000),
        "Done.",
        session_id="sess-req2",
    )

    assert len(enqueued) == 2
    assert enqueued[0]["event_kind"] == "reference_only"
    assert enqueued[0]["qualifies_nightly"] is True
    assert enqueued[1]["event_kind"] == "human_turn"
    assert enqueued[1]["qualifies_nightly"] is True


# --- 需求 3：dead nightly 任务自动复位且不反复复位 ---

def test_req3_dead_nightly_job_requeued_once_by_tick(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, max_attempts=1),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    # 写入落在 2026-11-19 窗口的事件（2026-11-18 23:00+08 ~ 2026-11-19 23:00+08）
    store.ingest_event(
        client="hermes",
        session_id="s1",
        event_id="n-1",
        content="白天讨论了一件事",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    # 推进时钟过 2026-11-19 23:00+08 (= 15:00 UTC)
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    t1 = store.tick()
    assert t1.jobs_created == 1
    assert t1.windows_requeued_dead == 0

    claimed = store.claim_job("w-dead")
    assert claimed is not None and claimed.job_type == "nightly"
    new_status = store.ack_job_failure(
        claimed.job_id,
        claimed.lease_token,
        error="ContextBudgetExceededError: old failure",
        force_dead=True,
    )
    assert new_status == "dead"

    # 第一次 tick：将 dead nightly 原位复位为 pending，计数 = 1，保留 last_error，写 receipt
    t2 = store.tick()
    assert t2.windows_requeued_dead == 1
    st = store.get_status()
    assert len(st["jobs"]) == 1
    job_info = st["jobs"][0]
    assert job_info["id"] == claimed.job_id
    assert job_info["status"] == "pending"
    assert job_info["attempts"] == 0
    assert "old failure" in (job_info["last_error"] or "")

    receipts = store.list_pending_receipts(client_id="hermes", session_id="s1")
    assert any(r["outcome"] == "retrying_after_dead" and "old failure" in (r["error_summary"] or "") for r in receipts)

    # 再次 tick：已是 pending，不再重复复位，计数 = 0
    t3 = store.tick()
    assert t3.windows_requeued_dead == 0

    # runner cycle 返回与日志均包含 windows_requeued_dead
    runner = ProactiveRunner(cfg, clock=clock)
    # 避免实际调模型
    cycle_res = runner.run_cycle(max_jobs=0)
    assert "windows_requeued_dead" in cycle_res["tick"]


# --- 需求 4：桥接脚本显式绑定 profile 默认模型 ---

def test_req4_bridge_uses_profile_default_model_and_records_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module(
        "hermes_review_bridge_req4",
        "agentbrain/integrations/hermes_review_bridge.py",
    )

    hermes_home = tmp_path / "hermes_profile"
    hermes_home.mkdir(parents=True)
    agent_dir = tmp_path / "hermes_agent"
    (agent_dir / "agent").mkdir(parents=True)

    # 写 config.yaml，同时设置 auxiliary.background_review.model 为别的值
    (hermes_home / "config.yaml").write_text(
        "\n".join([
            "model:",
            "  default: test-model-x",
            "  provider: test-provider-y",
            "  api_key: sk-should-never-leak-1234567890",
            "auxiliary:",
            "  background_review:",
            "    provider: wrong-aux-provider",
            "    model: wrong-aux-model",
        ]) + "\n",
        encoding="utf-8",
    )

    captured_calls: list[dict[str, Any]] = []

    fake_aux = ModuleType("agent.auxiliary_client")

    def fake_call_llm(**kwargs: Any) -> dict[str, Any]:
        captured_calls.append(dict(kwargs))
        ri = kwargs.get("route_info")
        if isinstance(ri, dict):
            ri["resolved_provider"] = kwargs.get("provider") or "aux-fallback-provider"
            ri["resolved_model"] = kwargs.get("model") or "aux-fallback-model"
        return {"text": json.dumps({"outcome": "no_change", "operations": []})}

    def fake_extract(resp: Any) -> str:
        return str(resp["text"])

    fake_aux.call_llm = fake_call_llm  # type: ignore[attr-defined]
    fake_aux.extract_content_or_reasoning = fake_extract  # type: ignore[attr-defined]
    fake_agent_pkg = ModuleType("agent")
    fake_agent_pkg.auxiliary_client = fake_aux  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)

    req_payload = {
        "hermes_agent_dir": str(agent_dir),
        "hermes_home": str(hermes_home),
        "system_prompt": "sys",
        "user_prompt": "usr",
        "timeout_seconds": 10.0,
        "max_output_tokens": 512,
        "temperature": 0.1,
    }
    fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(req_payload).encode("utf-8")))
    out_buf = io.BytesIO()
    fake_stdout = SimpleNamespace(buffer=out_buf)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    rc = bridge.main()
    assert rc == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["provider"] == "test-provider-y"
    assert captured_calls[0]["model"] == "test-model-x"

    raw_out = out_buf.getvalue().decode("utf-8")
    assert "sk-should-never-leak" not in raw_out
    parsed = json.loads(raw_out)
    assert parsed["ok"] is True
    assert parsed["route_source"] == "profile_default"
    assert parsed["route_resolved"] is True
    assert parsed["route"]["resolved_provider"] == "test-provider-y"
    assert parsed["route"]["resolved_model"] == "test-model-x"

    # config.yaml 缺失时回退框架路由并标注 framework_fallback
    missing_home = tmp_path / "missing_home"
    missing_home.mkdir()
    req_fallback = dict(req_payload, hermes_home=str(missing_home))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(req_fallback).encode("utf-8"))))
    out_buf2 = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out_buf2))
    assert bridge.main() == 0
    parsed2 = json.loads(out_buf2.getvalue().decode("utf-8"))
    assert parsed2["route_source"] == "framework_fallback"
    assert parsed2["route_resolved"] is False

    # worker 将 model_route 写入 result_summary_json 并在 get_status() 中可见
    clock = ManualClock(dt.datetime(2026, 11, 20, 2, 0, tzinfo=dt.timezone.utc))
    cfg = _make_cfg(tmp_path / "worker_route", proactive=ProactiveConfig(enabled=True))
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-route", event_id=f"r-{i}", content=f"msg {i}")

    def fake_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {"outcome": "no_change", "operations": []},
            "route": {
                "route_source": "profile_default",
                "route_resolved": True,
                "resolved_provider": "test-provider-y",
                "resolved_model": "test-model-x",
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=fake_runner)
    assert res is not None
    st = store.get_status()
    assert st["last_model_route"]["route_source"] == "profile_default"
    assert st["last_model_route"]["resolved_model"] == "test-model-x"
    assert st["jobs"][0]["model_route"]["resolved_provider"] == "test-provider-y"


# --- 需求 5：人格与「怎么写」分离 ---

def test_req5_soul_loading_and_prompt_separation(tmp_path: Path) -> None:
    soul_file = tmp_path / "SOUL.md"
    soul_file.write_text("---\nprofile: lily\n---\n# Persona\nSOUL-V1\n", encoding="utf-8")
    assert load_soul(soul_file) == "# Persona\nSOUL-V1"

    time.sleep(0.02)
    soul_file.write_text("# Persona\nSOUL-V2-UPDATED\n", encoding="utf-8")
    assert load_soul(soul_file) == "# Persona\nSOUL-V2-UPDATED"

    with pytest.raises(PromptAssetError):
        load_soul(tmp_path / "MISSING_SOUL.md")

    empty_soul = tmp_path / "EMPTY.md"
    empty_soul.write_text("---\nk: v\n---\n   \n", encoding="utf-8")
    with pytest.raises(PromptAssetError):
        load_soul(empty_soul)

    inc = _system_prompt("incremental", soul_text="SOUL-PROBE")
    night = _system_prompt("nightly", window_id="2026-11-19", soul_text="SOUL-PROBE")
    assert "SOUL-PROBE" in inc
    assert "SOUL-PROBE" in night
    for forbidden in ("银白长发", "星形耳钉"):
        assert forbidden not in inc
        assert forbidden not in night


# --- 需求 6：14 天遗忘与归档机制 ---

def test_req6_archive_14_day_policy_access_tracking_and_nightly_hook(tmp_path: Path) -> None:
    # 配置默认 fail-closed
    cfg_dir = tmp_path / "cfg_check" / "profiles" / "lily"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "memory.yaml").write_text("schema: 1\nprofile: lily\n", encoding="utf-8")
    mem_cfg, _ = load_memory_config(tmp_path / "cfg_check", "lily")
    assert mem_cfg.proactive.archive_enabled is False
    assert mem_cfg.proactive.archive_after_days == 14

    now_utc = dt.datetime(2026, 11, 20, 15, 5, tzinfo=dt.timezone.utc)
    now_us = dt_to_us(now_utc)
    day_sec = 86400

    cfg = _make_cfg(
        tmp_path / "arch",
        proactive=ProactiveConfig(enabled=True, archive_enabled=True, archive_after_days=14),
    )
    facts_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    file_a = facts_dir / "old_unused.md"
    file_b = facts_dir / "recent_note.md"
    file_c = facts_dir / "old_but_accessed.md"
    file_a.write_text("---\ntype: fact\ntitle: Old Unused\n---\nold unused content alpha\n", encoding="utf-8")
    file_b.write_text("---\ntype: fact\ntitle: Recent Note\n---\nrecent content beta\n", encoding="utf-8")
    file_c.write_text("---\ntype: fact\ntitle: Old Accessed\n---\nold accessed content gamma\n", encoding="utf-8")

    # 让 HOT.md 也是新鲜的，不被归档
    hot_file = cfg.profile_dir / "10_Working" / "HOT.md"
    recent_ts = now_utc.timestamp() - 2 * day_sec
    old_ts = now_utc.timestamp() - 20 * day_sec
    os.utime(hot_file, (recent_ts, recent_ts))
    os.utime(file_a, (old_ts, old_ts))
    os.utime(file_b, (now_utc.timestamp() - 5 * day_sec, now_utc.timestamp() - 5 * day_sec))
    os.utime(file_c, (old_ts, old_ts))

    reindex(cfg)
    # 刷新 file_c 的访问时间为 2 天前
    two_days_ago_us = dt_to_us(now_utc - dt.timedelta(days=2))
    touch_notes_access(cfg, ["20_LongTerm/Facts/old_but_accessed.md"], now_us=two_days_ago_us)

    # reindex 不得清零 last_access_us
    reindex(cfg)
    with connect_db(cfg.broker.db_path) as conn:
        row_c = conn.execute(
            "SELECT last_access_us FROM notes WHERE path = ?",
            ("20_LongTerm/Facts/old_but_accessed.md",),
        ).fetchone()
        assert row_c is not None and int(row_c["last_access_us"]) == two_days_ago_us

    # search_notes 命中后刷新 last_access_us
    search_notes(cfg, "recent content beta")
    with connect_db(cfg.broker.db_path) as conn:
        row_b = conn.execute(
            "SELECT last_access_us FROM notes WHERE path = ?",
            ("20_LongTerm/Facts/recent_note.md",),
        ).fetchone()
        assert row_b is not None and int(row_b["last_access_us"]) > 0

    plan = plan_archive(cfg, now_us, days=14)
    assert [c.rel_path for c in plan] == ["20_LongTerm/Facts/old_unused.md"]

    store = ProactiveStore.from_app_config(cfg, clock=ManualClock(now_utc))
    records = apply_archive(cfg, plan, store=store, now_utc=now_utc)
    assert len(records) == 1
    assert records[0].note_path == "20_LongTerm/Facts/old_unused.md"
    assert records[0].archived_path == "90_Archive/20_LongTerm/Facts/old_unused.md"

    assert not file_a.exists()
    assert file_b.exists()
    assert file_c.exists()
    assert (cfg.profile_dir / "90_Archive" / "20_LongTerm" / "Facts" / "old_unused.md").exists()

    # 索引行已删除
    with connect_db(cfg.broker.db_path) as conn:
        gone = conn.execute(
            "SELECT 1 FROM notes WHERE path = ?",
            ("20_LongTerm/Facts/old_unused.md",),
        ).fetchone()
        assert gone is None

    # archive_log 有且仅有 (a) 一行
    with sqlite3.connect(str(cfg.proactive_db_path)) as pconn:
        rows = pconn.execute("SELECT note_path, archived_path, archive_version FROM archive_log").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("20_LongTerm/Facts/old_unused.md", "90_Archive/20_LongTerm/Facts/old_unused.md", 1)

    # audit/archive.jsonl 已写入
    audit_lines = (cfg.broker.audit_dir / "archive.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["note_path"] == "20_LongTerm/Facts/old_unused.md"


def test_req6_nightly_archive_failure_does_not_fail_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path / "nightly_arch_fail",
        proactive=ProactiveConfig(enabled=True, archive_enabled=True, archive_after_days=14),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-nf",
        event_id="nf-1",
        content="白天讨论了归档异常隔离",
        occurred_at=dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    def boom_plan(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated disk error during archive")

    monkeypatch.setattr("agentbrain.proactive_worker.plan_archive", boom_plan)

    def fake_nightly_runner(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "event_date": "2026-11-19",
                        "content": "今天和搭档把夜间归档异常隔离的逻辑理顺了，就算归档报错也不会拖垮复盘。",
                        "source_refs": ["nf-1"],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "event_date": "2026-11-19",
                        "content": "把归档放在 ack 之后心里踏实多了，至少不会因为磁盘小毛病丢掉一整天的记忆整理。",
                        "source_refs": ["nf-1"],
                    },
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=fake_nightly_runner)
    assert res is not None and res.outcome == "applied"
    st = store.get_status()
    assert st["jobs"][0]["status"] == "succeeded"
    receipts = store.list_pending_receipts(client_id="hermes", session_id="s-nf")
    assert any(r["outcome"] == "archive_failed" and "simulated disk error" in (r["error_summary"] or "") for r in receipts)


# --- 需求 7：新建文件命名统一（不追溯已有文件） ---

def test_req7_unified_new_note_naming_and_fact_key_preservation(tmp_path: Path) -> None:
    assert normalize_note_title("测试：全角冒号，和 空格（括号）") == "测试-全角冒号-和-空格-括号"
    assert normalize_note_title("   :::   ") == "note"
    assert len(normalize_note_title("这是一个非常非常非常非常非常长的中文标题超过二十个字")) <= 20

    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 20, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-name", event_id=f"nm-{i}", content=f"fact {i}")
    job = store.claim_job("w-name")
    assert job is not None
    events = store.get_job_events(job.job_id)

    # 既有 fact_key 笔记（旧文件名）
    old_note = cfg.profile_dir / "20_LongTerm" / "Facts" / "旧名字.md"
    old_note.write_text(
        "---\ntype: fact\ntitle: 旧名字\nfact_key: legacy.slot\nproactive_owned: true\n---\n\n# 旧名字\n\n<!-- agentbrain:managed:start -->\nold\n<!-- agentbrain:managed:end -->\n",
        encoding="utf-8",
    )

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
                    "title": "标题：含全角冒号 和空格",
                    "content": "新事实正文",
                    "event_date": "2026-11-20",
                    "source_refs": ["nm-1"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "decision",
                    "title": "采用新命名规范",
                    "content": "决策正文",
                    "event_date": "2026-11-20",
                    "source_refs": ["nm-2"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "entity",
                    "title": "AgentBrain",
                    "content": "实体正文",
                    "source_refs": ["nm-3"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "moc",
                    "title": "记忆系统索引",
                    "content": "MOC 正文",
                    "source_refs": ["nm-4"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "换了新标题但不该新建",
                    "fact_key": "legacy.slot",
                    "content": "更新到旧文件",
                    "source_refs": ["nm-5"],
                },
                {
                    "op_type": "project_update",
                    "project": "新项目",
                    "managed_content": "项目进展",
                    "source_refs": ["nm-1"],
                },
            ],
        },
        clock=clock,
    )
    assert summary.applied_count == 6
    assert (cfg.profile_dir / "20_LongTerm" / "Facts" / "FACT-01-标题-含全角冒号-和空格.md").exists()
    assert (cfg.profile_dir / "20_LongTerm" / "Decisions" / "ADR-01-采用新命名规范.md").exists()
    assert (cfg.profile_dir / "20_LongTerm" / "Entities" / "ENTITY-AgentBrain.md").exists()
    assert (cfg.profile_dir / "20_LongTerm" / "MOC" / "MOC-记忆系统索引.md").exists()
    assert (cfg.profile_dir / "20_LongTerm" / "Projects" / "PROJ-01-新项目.md").exists()

    # fact_key 命中旧文件：不新建，原地更新
    assert "更新到旧文件" in old_note.read_text(encoding="utf-8")
    fact_files = [p.name for p in (cfg.profile_dir / "20_LongTerm" / "Facts").glob("*.md")]
    assert len(fact_files) == 2


# --- 需求 4 补充：运行期解释器没有 PyYAML（只有 ruamel 或都没有）时仍要解析出 profile 默认模型 ---

def test_req4_bridge_profile_model_parses_without_pyyaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load_module("hermes_review_bridge_noyaml", "agentbrain/integrations/hermes_review_bridge.py")

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: runtime-model-z\n"
        "  provider: runtime-provider-w\n"
        "  base_url: https://example.invalid/v1\n"
        "  api_mode: chat_completions\n"
        "providers:\n"
        "  other:\n"
        "    model: should-not-win\n",
        encoding="utf-8",
    )

    # 直接把两个 YAML 库都伪装成不可用，模拟 Hermes 受管解释器的真实环境
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)

    parsed = bridge._read_profile_default_model(str(home))
    assert parsed is not None
    assert parsed["model"] == "runtime-model-z"
    assert parsed["provider"] == "runtime-provider-w"
    assert parsed["base_url"] == "https://example.invalid/v1"
    assert parsed["api_mode"] == "chat_completions"

    stub = bridge._parse_model_block_stub(
        "model: {default: inline-model, provider: inline-provider}\n"
    )
    assert stub == {"default": "inline-model", "provider": "inline-provider"}


# --- 需求 1 延伸：多分段夜间复盘只允许写一条公账 + 一条私账（不许一天写成七八条流水账） ---

def test_req1_multisegment_nightly_writes_exactly_one_diary_per_ledger(tmp_path: Path) -> None:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(2026, 11, 18, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(
        tmp_path / "multi_nightly",
        proactive=ProactiveConfig(enabled=True, model_max_context_chars=8000),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-multi",
            event_id=f"m-{i}",
            content=f"第 {i} 段记录：" + ("他今天在改夜间复盘链路。" * 120),
            occurred_at=dt.datetime(2026, 11, 19, 3 + i, 0, tzinfo=dt.timezone.utc),
        )
    clock.set_utc(dt.datetime(2026, 11, 19, 16, 0, tzinfo=dt.timezone.utc))
    store.tick()

    seg_calls: list[str] = []

    def runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        sys_prompt = str(req.get("system_prompt") or "")
        user_prompt = str(req.get("user_prompt") or "")
        if "夜间" not in sys_prompt:
            # 增量复盘（5 轮批次）任务：本用例只关心夜间，直接不动
            return {"ok": True, "content": {"outcome": "no_change", "operations": []}}
        is_segment = "分段" in sys_prompt
        seg_calls.append("segment" if is_segment else "final")
        # source_refs 必须引用当前分段里真实存在的事件，否则整批会被拒
        import re as _re

        m = _re.search(r"event_id=(\S+)", user_prompt)
        ref = m.group(1) if m else "m-1"
        if is_segment:
            # 故意不听话地写一条日记：代码必须把它剔掉
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "digest": f"分段轨迹 {len(seg_calls)}",
                    "operations": [
                        {
                            "op_type": "diary_append",
                            "ledger": "public",
                            "event_date": "2026-11-19",
                            "content": "这是一条分段轮次里不该出现的日记。",
                            "source_refs": [ref],
                        },
                        {
                            "op_type": "longterm_upsert",
                            "note_type": "fact",
                            "title": "分段里写下的一条事实",
                            "content": "让后续分段知道这条已经写过，避免重复建文件。",
                            "source_refs": [ref],
                        },
                    ],
                },
            }
        assert "<earlier_segment_digests>" in user_prompt
        assert "<already_written_notes>" in user_prompt
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "diary_append",
                        "ledger": "public",
                        "event_date": "2026-11-19",
                        "content": "整天合起来只该有一条公账：他把夜间复盘的分段、归档和命名一起收口了。",
                        "source_refs": [ref],
                    },
                    {
                        "op_type": "diary_append",
                        "ledger": "private",
                        "event_date": "2026-11-19",
                        "content": "写整天的账比写零碎的分段踏实，至少不用重复讲三遍同一件事。",
                        "source_refs": [ref],
                    },
                ],
            },
        }

    results = []
    while True:
        r = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
        if r is None:
            break
        results.append(r)
    assert results, "至少应处理完增量任务与夜间任务"
    assert any(r.outcome == "applied" for r in results)
    assert seg_calls.count("segment") >= 1
    assert seg_calls.count("final") == 1

    public_text = (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-19.md").read_text(encoding="utf-8")
    private_text = (cfg.profile_dir / "30_Diary" / "Private" / "2026-11-19.md").read_text(encoding="utf-8")
    assert public_text.count("- [") == 1, public_text
    assert private_text.count("- [") == 1, private_text
    assert "分段轮次里不该出现" not in public_text


# --- 需求 1 延伸 2：模型漏字段时给一次「只修格式」的重试，不整批判死 ---

def test_req1_batch_validation_error_triggers_one_repair_retry(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 20, 6, 0, tzinfo=dt.timezone.utc))
    cfg = _make_cfg(tmp_path / "repair")
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s-fix", event_id=f"fx-{i}", content=f"事实 {i}")

    calls: list[str] = []

    def runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        user_prompt = str(req.get("user_prompt") or "")
        if "没有通过服务端校验" in user_prompt:
            calls.append("repair")
            return {
                "ok": True,
                "content": {
                    "outcome": "operations",
                    "operations": [
                        {
                            "op_type": "longterm_upsert",
                            "note_type": "fact",
                            "title": "修复后的事实",
                            "content": "补齐 source_refs 之后才被接受。",
                            "source_refs": ["fx-1"],
                        }
                    ],
                },
            }
        calls.append("first")
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "longterm_upsert",
                        "note_type": "fact",
                        "title": "漏了字段的事实",
                        "content": "这条没有 source_refs，必须被服务端拒掉。",
                    }
                ],
            },
        }

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    assert res is not None
    assert calls == ["first", "repair"]
    assert res.applied_count == 1
    assert store.get_status()["jobs"][0]["status"] == "succeeded"
