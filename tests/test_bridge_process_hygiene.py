"""复盘桥接的进程与批次收敛回归：不留控制台窗口、空操作不整批作废。"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from agentbrain import proactive_hermes


def _load_bridge():
    path = Path("agentbrain/integrations/hermes_review_bridge.py")
    spec = importlib.util.spec_from_file_location("hermes_review_bridge_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bridge_spawns_without_console_window(monkeypatch: pytest.MonkeyPatch, isolated_env) -> None:
    """父进程是 pythonw 时，子进程缺 CREATE_NO_WINDOW 会闪一个空白控制台窗口。"""
    captured: dict[str, object] = {}

    class _FakeCompleted:
        stdout = json.dumps({"ok": True, "content": {"outcome": "no_change", "operations": []}})
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(proactive_hermes.subprocess, "run", fake_run)
    monkeypatch.setattr(proactive_hermes.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(proactive_hermes, "discover_hermes_python", lambda *a, **k: Path("python.exe"))

    from dataclasses import replace

    enabled_cfg = replace(
        isolated_env,
        memory=replace(isolated_env.memory, proactive=replace(isolated_env.memory.proactive, enabled=True)),
    )
    result = proactive_hermes.call_hermes_framework_model(enabled_cfg, "system", "user")
    assert result == {"outcome": "no_change", "operations": []}
    if os.name == "nt":
        assert captured.get("creationflags") == proactive_hermes.subprocess.CREATE_NO_WINDOW
    assert captured.get("shell") is False


def test_empty_operation_placeholder_is_dropped_not_fatal() -> None:
    bridge = _load_bridge()
    out = bridge._normalize_batch(
        {"outcome": "operations", "operations": [{"op_type": ""}, {"op_type": "   "}]}
    )
    assert out == {"outcome": "no_change", "operations": []}

    kept = bridge._normalize_batch(
        {"outcome": "operations", "operations": [{"op_type": ""}, {"op_type": "diary_append", "content": "x"}]}
    )
    assert kept["outcome"] == "operations"
    assert [op["op_type"] for op in kept["operations"]] == ["diary_append"]


def test_jsonl_salvage_skips_empty_op_type() -> None:
    bridge = _load_bridge()
    text = '{"op_type": ""}\n{"op_type": "handoff_upsert", "task_key": "k", "summary": "s"}\n{"op_type": "'
    out = bridge._normalize_batch(bridge._extract_json_object(text))
    assert out["outcome"] == "operations"
    assert [op["op_type"] for op in out["operations"]] == ["handoff_upsert"]
