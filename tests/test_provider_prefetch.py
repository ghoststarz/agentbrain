"""Hermes agentbrain 插件 prefetch 单元测试。

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

_RAW_PLUGIN_ENV = os.environ.get("AGENTBRAIN_PLUGIN_PATH", "").strip()
_REPO_PLUGIN_PATH = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "agentbrain" / "__init__.py"
_PLUGIN_PATH = _REPO_PLUGIN_PATH if _RAW_PLUGIN_ENV == "source" else Path(_RAW_PLUGIN_ENV or "__missing__")


def _load_provider_cls() -> type[Any]:
    if not _PLUGIN_PATH.exists():
        pytest.skip(f"plugin not found at {_PLUGIN_PATH}")
    spec = importlib.util.spec_from_file_location("agentbrain_plugin_plugin", _PLUGIN_PATH)
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
        assert "[MEMORY_INDEX]" in out
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
    assert "[MEMORY_INDEX]" in out3


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


def test_prefetch_no_diary_noise_when_no_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """无可用召回且无回执时保持静默，不注入 [DIARY] 或 diary_today 噪声。"""
    cls = _load_provider_cls()
    provider = cls()

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {
                "hits": [],
                "revision": 5,
                "diary_today": {
                    "public": {"count": 1, "last_time": "10:26:31", "last_author": "antigravity"},
                    "private": {"count": 0},
                },
            }
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    out = provider.prefetch("一个没有任何命中的查询")
    assert out == ""
    assert "[DIARY]" not in provider.system_prompt_block()
    assert provider.recall_status() is None


def test_prefetch_delivers_and_acks_review_receipt_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """无记忆命中但有待送达评审回执时，只返回一次性 [PREVIOUS_REVIEW_RESULT] 并立即 ack。"""
    cls = _load_provider_cls()
    provider = cls()
    acked: list[dict[str, Any]] = []
    receipts = [
        [
            {
                "id": 42,
                "job_type": "incremental",
                "outcome": "no_change",
                "applied_count": 0,
                "categories": {},
            }
        ],
        [],
    ]

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {"hits": [], "revision": 5, "review_receipts": receipts.pop(0)}
        if path == "/v1/review/receipts/ack":
            acked.append(dict(payload or {}))
            return {"ok": True, "acked": 1}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    out1 = provider.prefetch("闲聊", session_id="sess-1")
    assert "[PREVIOUS_REVIEW_RESULT]" in out1
    assert "no_change" in out1
    assert "[DIARY]" not in out1
    assert acked == [{"session_id": "sess-1", "receipt_ids": [42]}]

    out2 = provider.prefetch("下一轮", session_id="sess-1")
    assert out2 == ""


def test_prefetch_index_lines_stay_short_even_with_huge_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引模式只注入「文件名 + 摘要」，正文再长也不会撑爆预算。"""
    cls = _load_provider_cls()
    provider = cls()

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {
                "hits": [{"title": "巨长笔记", "excerpt": "字" * 2000}],
                "revision": 5,
            }
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    out = provider.prefetch("任意查询")
    assert out.startswith("[MEMORY_INDEX]")
    assert "巨长笔记" in out
    assert "字" * 200 not in out
    assert len(out) <= 1200


def test_prefetch_returns_empty_when_a_single_line_exceeds_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """单行就超预算时宁可空，也不吐半行。"""
    cls = _load_provider_cls()
    provider = cls()

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {"hits": [{"title": "长" * 2000, "summary": "短摘要"}], "revision": 5}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    out = provider.prefetch("任意查询")
    assert out == ""
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
