"""Hermes 框架后台评审路由、受管解释器发现、安全桥接与隔离性测试。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentbrain.config import (
    AppConfig,
    BrokerConfig,
    MemoryConfig,
    ProactiveConfig,
    ProactiveConfigError,
    load_memory_config,
    resolve_effective_model_backend,
    validate_proactive_worker_config,
)
from agentbrain.proactive import EVENT_KIND_HUMAN_TURN, ProactiveStore
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_hermes import (
    HermesBridgeError,
    build_hermes_bridge_env,
    call_hermes_framework_model,
    discover_hermes_python,
    get_hermes_review_bridge_path,
    resolve_hermes_profile_home,
)
from agentbrain.proactive_runner import ProactiveRunner
from agentbrain.proactive_worker import (
    ContextBudgetExceededError,
    ModelTransportError,
    run_worker_once,
)


def _make_cfg(
    tmp_path: Path,
    *,
    profile: str = "lily",
    proactive: ProactiveConfig | None = None,
) -> AppConfig:
    vault = tmp_path / "vault"
    prof = vault / "profiles" / profile
    for sub in (
        "00_Identity",
        "10_Working",
        "20_LongTerm/Facts",
        "20_LongTerm/Projects",
        "30_Diary/Public",
        "30_Diary/Private",
    ):
        (prof / sub).mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Lily\n", encoding="utf-8")
    (prof / "10_Working" / "HOT.md").write_text("# HOT\n\n- 初始\n", encoding="utf-8")
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
        log_dir=rt / "logs",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    return AppConfig(
        broker=broker,
        memory=MemoryConfig(
            profile=profile,
            proactive=proactive or ProactiveConfig(),
        ),
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=prof / "memory.yaml",
    )


def _seed_incremental_job(store: ProactiveStore, *, count: int = 5, content: str = "记住测试结论") -> None:
    for i in range(count):
        store.ingest_event(
            client="hermes",
            session_id="sess-hermes-1",
            event_id=f"ev-{i}",
            event_kind=EVENT_KIND_HUMAN_TURN,
            content=f"{content} #{i}",
            occurred_at=dt.datetime(2026, 11, 17, 6, 31 + i, 0, tzinfo=dt.timezone.utc),
        )


def test_default_backend_is_hermes_without_endpoint_or_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ProactiveConfig(enabled=True)
    assert resolve_effective_model_backend(cfg) == "hermes"
    validate_proactive_worker_config(cfg)

    # 兼容旧配置：显式或隐式使用 model_endpoint/model_name 仍走 openai_compatible
    legacy_ok = ProactiveConfig(enabled=True, model_endpoint="http://127.0.0.1:8080/v1", model_name="m1")
    assert resolve_effective_model_backend(legacy_ok) == "openai_compatible"
    validate_proactive_worker_config(legacy_ok)

    legacy_missing_ep = ProactiveConfig(enabled=True, model_endpoint="", model_name="gpt-test")
    assert resolve_effective_model_backend(legacy_missing_ep) == "openai_compatible"
    with pytest.raises(ProactiveConfigError):
        validate_proactive_worker_config(legacy_missing_ep)

    with pytest.raises(ProactiveConfigError):
        validate_proactive_worker_config(ProactiveConfig(enabled=True, model_backend="unknown_backend"))

    # memory.yaml 仅配置 enabled: true 时默认使用 hermes 路由，支持可移植环境变量
    monkeypatch.setenv("TEST_PORTABLE_HERMES", str(tmp_path / "portable-hermes"))
    vault = tmp_path / "vault"
    mem_dir = vault / "profiles" / "lily"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\nproactive:\n  enabled: true\n  hermes_home: \"${TEST_PORTABLE_HERMES}\"\n",
        encoding="utf-8",
    )
    loaded, _ = load_memory_config(vault, "lily")
    assert resolve_effective_model_backend(loaded.proactive) == "hermes"
    assert loaded.proactive.hermes_home.endswith("portable-hermes")


def test_discover_hermes_python_from_install_facts(tmp_path: Path) -> None:
    hermes_root = tmp_path / "hermes"
    agent_dir = hermes_root / "hermes-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    venv_dir = hermes_root / "installs" / "abc" / "environments" / "env1" / "venv"
    py_path = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
    py_path.parent.mkdir(parents=True, exist_ok=True)
    py_path.write_text("", encoding="utf-8")

    install_key = hashlib.sha256(str(agent_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    facts_path = hermes_root / "installs" / install_key / "facts.json"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    facts_path.write_text(
        json.dumps({"packages": {"venv": {"environment": str(venv_dir)}}}),
        encoding="utf-8",
    )

    found = discover_hermes_python(ProactiveConfig(enabled=True), hermes_root, agent_dir)
    assert found == py_path


def test_packaged_bridge_subprocess_success_error_and_stdout_isolation(tmp_path: Path) -> None:
    bridge = get_hermes_review_bridge_path()
    assert bridge.is_file()

    hermes_root = tmp_path / "hermes"
    agent_dir = hermes_root / "hermes-agent"
    (agent_dir / "agent").mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent" / "__init__.py").write_text("", encoding="utf-8")

    # 假装 Hermes auxiliary_client：验证 task/tools/stream/env 与 stdout 噪声隔离
    (agent_dir / "agent" / "auxiliary_client.py").write_text(
        """import json, os, sys
from types import SimpleNamespace

def extract_content_or_reasoning(resp):
    return resp.choices[0].message.content

def call_llm(**kwargs):
    print("noisy plugin stdout that must go to stderr")
    assert os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1"
    assert kwargs.get("task") == "background_review"
    assert kwargs.get("tools") is None
    assert kwargs.get("stream") is False
    user_msg = kwargs["messages"][1]["content"]
    if "TRIGGER_ERROR" in user_msg:
        raise RuntimeError("upstream failed with sk-fake-secret-999999")
    if "TRIGGER_BAD_JSON" in user_msg:
        content = "not-json-at-all"
    else:
        content = '```json\\n{"outcome": "no_change", "operations": []}\\n```'
    if isinstance(kwargs.get("route_info"), dict):
        kwargs["route_info"].update({"provider": "fake", "model": "fake-bg"})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
""",
        encoding="utf-8",
    )

    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            hermes_profile="lily",
            hermes_home=str(hermes_root),
            hermes_agent_dir=str(agent_dir),
            hermes_python=sys.executable,
            model_timeout_seconds=10.0,
        ),
    )

    ok_out = call_hermes_framework_model(cfg, "sys", "normal evidence")
    assert ok_out == {"outcome": "no_change", "operations": []}

    with pytest.raises(ModelTransportError) as err_exc:
        try:
            call_hermes_framework_model(cfg, "sys", "TRIGGER_ERROR")
        except HermesBridgeError as exc:
            raise ModelTransportError(str(exc)) from exc
    assert "sk-fake-secret" not in str(err_exc.value)
    assert "[REDACTED_API_KEY]" in str(err_exc.value)

    with pytest.raises(HermesBridgeError):
        call_hermes_framework_model(cfg, "sys", "TRIGGER_BAD_JSON")


def test_hermes_timeout_triggers_retry_wait_and_budget_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_root = tmp_path / "hermes"
    agent_dir = hermes_root / "hermes-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            hermes_home=str(hermes_root),
            hermes_agent_dir=str(agent_dir),
            hermes_python=sys.executable,
            model_timeout_seconds=5.0,
            # 系统提示词开销约 2.2k 字：预算低于它时任何事件都塞不下（不是本用例要测的场景）
            model_max_context_chars=4000,
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=ManualClock(dt.datetime(2026, 11, 17, 6, 30, 0, tzinfo=dt.timezone.utc)))
    _seed_incremental_job(store, count=5, content="短证据")

    def fake_timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="python", timeout=10.0)

    monkeypatch.setattr(subprocess, "run", fake_timeout)
    res = run_worker_once(cfg, store, worker_id="w-timeout")
    assert res is None
    st = store.get_status()
    assert st["jobs"][0]["status"] == "retry_wait"
    assert "超时" in (st["jobs"][0]["last_error"] or "")

    # 单条事件超出单段预算时确定性切分，不再整单失败（需求 1：证据不许丢，也不许挂死）
    called: list[int] = []
    store2 = ProactiveStore(tmp_path / "budget.db", clock=ManualClock(dt.datetime(2026, 11, 17, 6, 30, 0, tzinfo=dt.timezone.utc)))
    _seed_incremental_job(store2, count=5, content="x" * 2000)
    res2 = run_worker_once(
        cfg,
        store2,
        worker_id="w-budget",
        hermes_runner=lambda **kw: called.append(1) or {"outcome": "no_change", "operations": []},
    )
    assert res2 is not None
    assert res2.segments_processed >= 2
    assert called, "切分后必须照常调用 runner，不许因预算把整批证据判死"
    assert store2.get_status()["jobs"][0]["status"] == "succeeded"


def test_disabled_proactive_never_spawns_hermes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_spawn(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must never be called when proactive.enabled=False")

    monkeypatch.setattr(subprocess, "run", forbid_spawn)
    cfg = _make_cfg(tmp_path, proactive=ProactiveConfig(enabled=False))
    store = ProactiveStore.from_app_config(cfg, clock=ManualClock(dt.datetime(2026, 11, 17, 6, 30, 0, tzinfo=dt.timezone.utc)))
    _seed_incremental_job(store, count=5)

    assert run_worker_once(cfg, store) is None
    runner = ProactiveRunner(cfg, clock=ManualClock(dt.datetime(2026, 11, 17, 6, 30, 0, tzinfo=dt.timezone.utc)))
    assert runner.start() is False
    assert runner.run_cycle() == {"enabled": False, "skipped": True}


def test_profile_isolation_and_recursion_prevention(tmp_path: Path) -> None:
    hermes_root = tmp_path / "hermes"

    cfg_alice = _make_cfg(
        tmp_path,
        profile="alice",
        proactive=ProactiveConfig(enabled=True, hermes_home=str(hermes_root)),
    )
    profile_a, home_a = resolve_hermes_profile_home(cfg_alice)
    assert profile_a == "alice"
    assert home_a == (hermes_root / "profiles" / "alice").resolve()

    cfg_lily = _make_cfg(
        tmp_path,
        profile="alice",
        proactive=ProactiveConfig(
            enabled=True,
            hermes_profile="lily",
            hermes_home=str(hermes_root),
        ),
    )
    profile_l, home_l = resolve_hermes_profile_home(cfg_lily)
    assert profile_l == "lily"
    assert home_l == (hermes_root / "profiles" / "lily").resolve()

    env = build_hermes_bridge_env(profile_a, home_a)
    assert env["HERMES_HOME"] == str(home_a)
    assert env["HERMES_PROFILE"] == "alice"
    assert env["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert env["AGENTBRAIN_PROACTIVE_INTERNAL"] == "1"
    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env

    with pytest.raises(ProactiveConfigError):
        validate_proactive_worker_config(ProactiveConfig(enabled=True, hermes_profile="../escape"))

    # 正常 fake runner 成功端到端投影
    store = ProactiveStore.from_app_config(cfg_lily, clock=ManualClock(dt.datetime(2026, 11, 17, 6, 30, 0, tzinfo=dt.timezone.utc)))
    _seed_incremental_job(store, count=5)
    seen_envs: list[dict[str, str]] = []

    def fake_runner(**kwargs: Any) -> dict[str, Any]:
        seen_envs.append(dict(kwargs["env"]))
        return {
            "ok": True,
            "content": {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "longterm_upsert",
                        "note_type": "fact",
                        "title": "Hermes 框架路由验证",
                        "content": "后台评审复用 Hermes 框架认证与模型路由。",
                        "fact_key": "hermes-framework-routing",
                        "keys": ["Hermes", "框架路由", "background_review"],
                        "tags": ["hermes"],
                        "evidence_level": "report",
                        "source_refs": ["hermes:sess-hermes-1:ev-0"],
                    }
                ],
            },
        }

    res = run_worker_once(cfg_lily, store, hermes_runner=fake_runner)
    assert res is not None, store.get_status()
    assert res.outcome == "applied"
    assert res.applied_count == 1
    assert seen_envs[0]["HERMES_HOME"] == str(home_l)
    assert seen_envs[0]["AGENTBRAIN_PROACTIVE_INTERNAL"] == "1"
