"""Stage 2 单元测试与集成测试：ACL / Handoff / Diary / Render / HTTP Server / MCP / Weak Score。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentbrain.acl import AuthError, authenticate_bearer, hash_token
from agentbrain.config import AppConfig, load_config
from agentbrain.diary import DiaryGateError, append_diary, read_diary
from agentbrain.handoff import list_open_handoffs, read_handoff, upsert_handoff
from agentbrain.index import init_db, reindex
from agentbrain.mcp_server import create_mcp_server
from agentbrain.render import render_boot, render_delta
from agentbrain.retrieve import search_notes
from agentbrain.server import create_http_server


@pytest.fixture()
def stage2_env(isolated_env: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AppConfig, Path, str]:
    """扩展 isolated_env：补全 clients/inject 配置、SOUL 小节、HOT.md 与 tokens.json。"""
    mem_path = isolated_env.memory_config_path
    mem_raw = yaml.safe_load(mem_path.read_text(encoding="utf-8"))
    mem_raw["clients"] = {
        "hermes:lily": {"domain": "work", "can_casual": True},
        "codex": {"domain": "work"},
        "antigravity": {"domain": "work"},
    }
    mem_raw["inject"] = {
        "hermes": {"static_chars": 450, "prefetch_chars": 1200},
        "codex": {"boot_chars": 2400, "delta_chars": 300},
        "antigravity": {"boot_chars": 2400, "delta_chars": 300},
    }
    mem_path.write_text(yaml.safe_dump(mem_raw, allow_unicode=True), encoding="utf-8")

    isolated_env.broker.soul_path.write_text(
        "# SOUL\n## 人格\n### 特质\n- 机敏、清醒、直给。\n### 语气与表达规范\n- 默认中文，简洁直接。\n",
        encoding="utf-8",
    )
    hot_dir = isolated_env.profile_dir / "10_Working"
    hot_dir.mkdir(parents=True, exist_ok=True)
    (hot_dir / "HOT.md").write_text(
        "---\ntype: fact\ncreated: 2026-11-14\n---\n# HOT\n- 当前阶段：Stage 2 服务面联调。\n",
        encoding="utf-8",
    )

    note_path = isolated_env.profile_dir / "20_LongTerm" / "Facts" / "幂等设计.md"
    note_path.write_text(
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 幂等设计\n写入与 reindex 均保持幂等。\n",
        encoding="utf-8",
    )

    cfg = load_config(broker_path=isolated_env.broker_config_path, profile="lily")
    init_db(cfg)
    reindex(cfg)

    raw_token = "test-secret-token-lily"
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps({hash_token(raw_token): "codex"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTBRAIN_TOKENS_PATH", str(tokens_path))
    return cfg, tokens_path, raw_token


def _http_call(url: str, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_acl_fail_closed_and_auth(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, tokens_path, raw_token = stage2_env

    ident = authenticate_bearer(f"Bearer {raw_token}", cfg, tokens_path=tokens_path)
    assert ident.client_id == "codex"
    assert ident.domain == "work"

    with pytest.raises(AuthError) as exc_bad:
        authenticate_bearer("Bearer WRONG", cfg, tokens_path=tokens_path)
    assert exc_bad.value.status_code == 403

    tokens_path.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(AuthError) as exc_corrupt:
        authenticate_bearer(f"Bearer {raw_token}", cfg, tokens_path=tokens_path)
    assert exc_corrupt.value.status_code == 503

    tokens_path.unlink()
    with pytest.raises(AuthError) as exc_missing:
        authenticate_bearer(f"Bearer {raw_token}", cfg, tokens_path=tokens_path)
    assert exc_missing.value.status_code == 503


def test_handoff_slots_and_open_filter(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, _, _ = stage2_env
    assert read_handoff(cfg) == {"tasks": {}}

    upsert_handoff(cfg, task_key="t1", status="open", summary="task 1", next_steps=["a"], touched_files=["f1.py"])
    upsert_handoff(cfg, task_key="t2", status="closed", summary="task 2")
    upsert_handoff(cfg, task_key="t3", status="in_progress", summary="task 3")
    upsert_handoff(cfg, task_key="t4", status="open", summary="task 4")
    upsert_handoff(cfg, task_key="t5", status="open", summary="task 5")

    open_list = list_open_handoffs(cfg, limit=3)
    assert len(open_list) == 3
    assert all(item["status"] != "closed" for item in open_list)


def test_diary_append_read_and_gate(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, _, _ = stage2_env
    res_pub = append_diary(cfg, ledger="public", content="公账测试记录", author="codex")
    assert res_pub["ok"] is True

    res_priv = append_diary(cfg, ledger="private", content="私密日记正文SECRET_XYZ", author="hermes:lily")
    assert res_priv["ok"] is True

    read_priv = read_diary(cfg, ledger="private", recent_days=3)
    assert len(read_priv["entries"]) == 1
    assert "SECRET_XYZ" in read_priv["entries"][0]["content"]

    # 开启 diary.gate 后，非 can_casual 客户端不可读私账
    mem_raw = yaml.safe_load(cfg.memory_config_path.read_text(encoding="utf-8"))
    mem_raw["diary"]["gate"] = True
    cfg.memory_config_path.write_text(yaml.safe_dump(mem_raw, allow_unicode=True), encoding="utf-8")
    gated_cfg = load_config(broker_path=cfg.broker_config_path, profile="lily")
    with pytest.raises(DiaryGateError):
        read_diary(gated_cfg, ledger="private", can_casual=False)


def test_render_boot_excludes_diary_and_respects_format(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, _, _ = stage2_env
    append_diary(cfg, ledger="private", content="绝密私账内容DO_NOT_LEAK", author="hermes:lily")
    append_diary(cfg, ledger="public", content="公账内容DO_NOT_INJECT", author="codex")
    upsert_handoff(cfg, task_key="stage2", status="in_progress", summary="验证启动块格式")

    boot = render_boot(cfg, target="codex")
    text = boot["text"]
    assert text.startswith("[SHARED_BRAIN profile=lily rev=")
    assert "## 身份" in text
    assert "## 热记忆" in text
    assert "## 待续" in text
    assert "DO_NOT_LEAK" not in text
    assert "DO_NOT_INJECT" not in text
    assert len(text) <= 2400

    delta_same = render_delta(cfg, since_rev=boot["revision"])
    assert delta_same["changed"] is False


def test_http_server_endpoints_and_fail_closed(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, tokens_path, raw_token = stage2_env
    httpd = create_http_server(cfg, host="127.0.0.1", port=0, tokens_path=tokens_path)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        st, body = _http_call(f"{base}/healthz")
        assert st == 200 and body["ok"] is True and isinstance(body["revision"], int)

        st, body = _http_call(f"{base}/v1/search", method="POST", payload={"query": "幂等"}, token="WRONG")
        assert st == 403 and body["error"] == "unauthenticated"

        st, body = _http_call(f"{base}/v1/search", method="POST", payload={"query": "幂等"}, token=raw_token)
        assert st == 200 and len(body["hits"]) >= 1

        st, body = _http_call(f"{base}/v1/boot", method="POST", payload={"target": "codex"}, token=raw_token)
        assert st == 200 and body["text"].startswith("[SHARED_BRAIN profile=lily rev=")

        st, body = _http_call(
            f"{base}/v1/write",
            method="POST",
            payload={"content": "新增测试事实内容", "type": "fact", "tags": ["test"]},
            token=raw_token,
        )
        assert st == 200 and body["ok"] is True and body["orphan_warning"] is True

        st, body = _http_call(f"{base}/v1/status", method="GET", token=raw_token)
        assert st == 200 and body["notes"] >= 2

        # 删除 tokens.json -> 503 失败即拒
        tokens_path.unlink()
        st, body = _http_call(f"{base}/v1/search", method="POST", payload={"query": "幂等"}, token=raw_token)
        assert st == 503 and body["error"] == "auth_unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_server_registers_all_tools_with_annotations(stage2_env: tuple[AppConfig, Path, str]) -> None:
    cfg, _, _ = stage2_env
    mcp = create_mcp_server(cfg, client_id="codex")
    tools = mcp._tool_manager.list_tools()  # type: ignore[attr-defined]
    names = {t.name for t in tools}
    expected = {
        "memory_boot",
        "memory_delta",
        "memory_search",
        "memory_read",
        "memory_write",
        "handoff_read",
        "handoff_upsert",
        "diary_append",
        "diary_read",
        "memory_status",
    }
    assert expected == names
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is not None
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is not None


def test_weak_evidence_score_nonzero(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    (d / "身高一.md").write_text(
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 身高一\n身高 175cm。\n",
        encoding="utf-8",
    )
    (d / "身高二.md").write_text(
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 身高二\n身高 175cm。\n",
        encoding="utf-8",
    )
    reindex(isolated_env)
    res = search_notes(isolated_env, "身高")
    assert res.get("evidence_gate") == "bypassed"
    assert len(res["hits"]) == 2
    assert all(hit["score"] > 0.0 for hit in res["hits"])
