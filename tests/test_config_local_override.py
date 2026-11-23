"""M1-a（memory.local.yaml 深度覆盖层）与 M1-b（proactive.nightly_enabled）测试。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from agentbrain.config import (
    ProactiveConfig,
    ProactiveConfigError,
    load_config,
    load_memory_config,
)
from agentbrain.proactive import EVENT_KIND_HUMAN_TURN, ProactiveStore
from agentbrain.proactive_clock import ManualClock


def _setup_minimal_env(tmp_path: Path, *, broker_extra: dict | None = None, memory_extra: dict | None = None) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    pdir.mkdir(parents=True, exist_ok=True)
    mem_data = {
        "schema": 1,
        "profile": "lily",
        "retrieval": {"seed_k": 5, "hops": 1, "max_chars": 1200},
        "working": {"hot_max_chars": 1500},
        "proactive": {
            "enabled": True,
            "event_capture": True,
            "incremental_batch_size": 5,
            "nightly_hour": 23,
            "nightly_minute": 0,
            "agy_ide_roots": [str(tmp_path / "ide_a"), str(tmp_path / "ide_b")],
        },
    }
    if memory_extra:
        mem_data.update(memory_extra)
    (pdir / "memory.yaml").write_text(yaml.safe_dump(mem_data, allow_unicode=True), encoding="utf-8")

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("# SOUL\n", encoding="utf-8")
    broker_data = {
        "vault_root": str(vault),
        "soul_path": str(soul),
        "port": 18765,
        "role": "primary",
        "agy_rules_path": str(tmp_path / "GEMINI.md"),
        "index_dir": str(runtime / "index"),
        "rev_dir": str(runtime / "rev"),
        "state_dir": str(runtime / "state"),
        "audit_dir": str(runtime / "audit"),
        "log_dir": str(runtime / "logs"),
    }
    if broker_extra:
        broker_data.update(broker_extra)
    broker_path = runtime / "broker.yaml"
    broker_path.write_text(yaml.safe_dump(broker_data, allow_unicode=True), encoding="utf-8")
    return broker_path, vault


def test_local_override_missing_is_noop(tmp_path: Path) -> None:
    broker_path, _ = _setup_minimal_env(tmp_path)
    cfg = load_config(broker_path=broker_path, profile="lily")
    assert cfg.memory.proactive.enabled is True
    assert cfg.memory.proactive.nightly_enabled is True
    assert cfg.memory.proactive.nightly_hour == 23
    assert cfg.memory.retrieval.seed_k == 5
    assert len(cfg.memory.proactive.agy_ide_roots) == 2


def test_local_override_default_sibling_deep_merge(tmp_path: Path) -> None:
    broker_path, _ = _setup_minimal_env(tmp_path)
    local_yaml = broker_path.parent / "memory.local.yaml"
    override_root = tmp_path / "ide_only_local"
    local_yaml.write_text(
        yaml.safe_dump(
            {
                "retrieval": {"seed_k": 9},
                "proactive": {
                    "nightly_enabled": False,
                    "agy_ide_roots": [str(override_root)],
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    cfg = load_config(broker_path=broker_path, profile="lily")
    # dict 逐层合并：未覆盖的键保持原值
    assert cfg.memory.retrieval.seed_k == 9
    assert cfg.memory.retrieval.hops == 1
    assert cfg.memory.proactive.enabled is True
    assert cfg.memory.proactive.nightly_hour == 23
    # 标量与 list 整体替换
    assert cfg.memory.proactive.nightly_enabled is False
    assert cfg.memory.proactive.agy_ide_roots == (override_root.resolve(),)


def test_local_override_custom_path_via_broker_yaml(tmp_path: Path) -> None:
    custom_local = tmp_path / "custom" / "override.yaml"
    custom_local.parent.mkdir(parents=True, exist_ok=True)
    custom_local.write_text("proactive:\n  nightly_enabled: false\n  incremental_batch_size: 8\n", encoding="utf-8")

    broker_path, _ = _setup_minimal_env(tmp_path, broker_extra={"memory_local_path": str(custom_local)})
    cfg = load_config(broker_path=broker_path, profile="lily")
    assert cfg.memory.proactive.nightly_enabled is False
    assert cfg.memory.proactive.incremental_batch_size == 8
    assert cfg.memory.proactive.enabled is True


def test_local_override_strict_validation_rejects_unknown_key_and_bad_bool(tmp_path: Path) -> None:
    broker_path, vault = _setup_minimal_env(tmp_path)
    local_yaml = broker_path.parent / "memory.local.yaml"

    # 未知键报错
    local_yaml.write_text("proactive:\n  nightly_enable_typo: false\n", encoding="utf-8")
    with pytest.raises(ProactiveConfigError, match="未知配置键"):
        load_config(broker_path=broker_path, profile="lily")

    # 非布尔值报错
    local_yaml.write_text("proactive:\n  nightly_enabled: 'false'\n", encoding="utf-8")
    with pytest.raises(ProactiveConfigError, match="必须为布尔值"):
        load_memory_config(vault, profile="lily", local_override_path=local_yaml)


def test_nightly_disabled_skips_windows_but_keeps_incremental(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 20, 12, 0, 0, tzinfo=dt.timezone.utc))
    cfg = ProactiveConfig(
        enabled=True,
        event_capture=True,
        incremental_batch_size=5,
        nightly_hour=23,
        nightly_minute=0,
        nightly_enabled=False,
    )
    store = ProactiveStore(tmp_path / "proactive.db", config=cfg, clock=clock)

    # 写入 5 轮人类对话，验证增量 5 轮复盘正常创建 incremental job
    created_inc_jobs: list[int] = []
    for i in range(1, 6):
        res = store.ingest_event(
            client="hermes",
            session_id="s-inc",
            event_id=f"ev-{i}",
            event_kind=EVENT_KIND_HUMAN_TURN,
            content=f"User: 第 {i} 轮\nAssistant: 回复 {i}",
            occurred_at=clock.now_utc() + dt.timedelta(minutes=i),
        )
        if res.created_job_id is not None:
            created_inc_jobs.append(res.created_job_id)
    assert len(created_inc_jobs) == 1

    # 时间推进两天后跑 tick()，不得创建任何夜间窗口或 nightly 任务
    clock.advance(days=2)
    tick_res = store.tick(include_historical=True)
    assert tick_res.windows_checked == 0
    assert tick_res.windows_skipped_empty == 0
    assert tick_res.jobs_created == 0
    assert tick_res.windows_reopened == 0
    assert tick_res.windows_requeued_dead == 0

    status = store.get_status()
    assert status["nightly_windows"] == []
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["job_type"] == "incremental"
    assert status["jobs"][0]["status"] == "pending"
