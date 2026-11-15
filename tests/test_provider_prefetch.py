"""Hermes obsidian-broker 插件 prefetch 单元测试。

插件本身不在本仓库（它是宿主侧的适配层）。设置环境变量
``AGENTBRAIN_PLUGIN_PATH`` 指向插件的 ``__init__.py`` 才会运行这组测试，
否则整体 skip。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_PATH = Path(os.environ.get("AGENTBRAIN_PLUGIN_PATH", "").strip() or "__missing__")


def _load_provider_cls() -> type[Any]:
    if not _PLUGIN_PATH.exists():
        pytest.skip(f"plugin not found at {_PLUGIN_PATH}")
    spec = importlib.util.spec_from_file_location("obsidian_broker_plugin", _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ObsidianBrokerMemoryProvider


def test_prefetch_runs_every_turn_when_revision_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _load_provider_cls()
    provider = cls()
    search_calls: list[str] = []

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/healthz":
            return {"ok": True, "revision": 7}
        if path == "/v1/search":
            search_calls.append(str((payload or {}).get("query") or ""))
            return {"hits": [{"title": "ADR-03", "excerpt": "同步器是中枢"}]}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    for q in ("ADR-03 的结论", "同步器 中枢", "幂等 记忆"):
        out = provider.prefetch(q)
        assert "[MEMORY_PREFETCH]" in out
        assert "[MEMORY_DELTA]" not in out
        assert "ADR-03" in out

    assert search_calls == ["ADR-03 的结论", "同步器 中枢", "幂等 记忆"]


def test_prefetch_injects_memory_delta_only_on_revision_change(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _load_provider_cls()
    provider = cls()
    revisions = iter([10, 12, 12])

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        # revision 现在随 /v1/search 一起返回（不再单独探活 /healthz）
        if path == "/v1/search":
            return {"hits": [{"title": "ADR-03", "excerpt": "同步器是中枢"}], "revision": next(revisions)}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    out1 = provider.prefetch("q1")
    out2 = provider.prefetch("q2")
    out3 = provider.prefetch("q3")

    assert "[MEMORY_DELTA]" not in out1
    assert "[MEMORY_DELTA]" in out2
    assert "10 -> 12" in out2
    assert "[MEMORY_DELTA]" not in out3
    assert "[MEMORY_PREFETCH]" in out3


def test_prefetch_returns_empty_when_all_hits_weak_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _load_provider_cls()
    provider = cls()

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/healthz":
            return {"ok": True, "revision": 1}
        if path == "/v1/search":
            return {
                "hits": [
                    {"title": "笔记A", "excerpt": "弱证据", "weak_evidence": True},
                    {"title": "笔记B", "excerpt": "弱证据", "weak_evidence": True},
                ]
            }
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    assert provider.prefetch("任意查询") == ""
    assert provider.recall_status() is None


def test_prefetch_forwards_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _load_provider_cls()
    provider = cls()
    payloads: list[dict[str, Any]] = []

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/healthz":
            return {"ok": True, "revision": 3}
        if path == "/v1/search":
            payloads.append(dict(payload or {}))
            return {"hits": [{"title": "ADR-03", "excerpt": "同步器是中枢"}]}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    provider.prefetch("ADR-03 的结论", session_id="sess-explicit")
    provider.initialize("sess-init")
    monkeypatch.setattr(provider, "_http_json", fake_http)
    provider.prefetch("ADR-03 的结论")

    assert payloads[0]["session_id"] == "sess-explicit"
    assert payloads[1]["session_id"] == "sess-init"
