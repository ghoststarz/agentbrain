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
from agentbrain.diary import DiaryGateError, append_diary, read_diary, today_diary_status
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


def test_today_diary_status_reports_counts_without_body(stage2_env: tuple[AppConfig, Path, str]) -> None:
    """今日日记状态只回报条数与作者；正文一个字都不能进状态结构。"""
    cfg, _, _ = stage2_env
    empty = today_diary_status(cfg, "public")
    assert empty["count"] == 0 and empty["last_time"] == ""

    append_diary(cfg, ledger="public", content="第一条约会日记BODY_ONE", author="antigravity")
    append_diary(cfg, ledger="public", content="第二条BODY_TWO", author="hermes:lily")
    append_diary(cfg, ledger="private", content="私账BODY_PRIVATE", author="hermes:lily")

    pub = today_diary_status(cfg, "public")
    assert pub["count"] == 2
    assert pub["last_author"] == "hermes"
    assert len(pub["last_time"]) == 8

    pri = today_diary_status(cfg, "private")
    assert pri["count"] == 1

    blob = repr(pub) + repr(pri)
    for leak in ("BODY_ONE", "BODY_TWO", "BODY_PRIVATE"):
        assert leak not in blob


def test_agy_rules_sync_is_idempotent_and_atomic(
    stage2_env: tuple[AppConfig, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agi 规则文件：内容没变就不写盘（mtime 不动），变了才原子替换。"""
    from agentbrain import agy_rules

    cfg, _, _ = stage2_env
    target = tmp_path / "gemini" / "GEMINI.md"
    monkeypatch.setattr(agy_rules, "agy_rules_path", lambda config=None: target)

    first = agy_rules.sync_agy_rules(cfg)
    assert first["changed"] is True and target.exists()
    mtime1 = target.stat().st_mtime_ns

    second = agy_rules.sync_agy_rules(cfg)
    assert second["changed"] is False
    assert target.stat().st_mtime_ns == mtime1, "内容没变却重写了文件（会制造无谓噪音/竞态）"

    # 热记忆变了 -> 必须重渲染
    hot = cfg.profile_dir / "10_Working" / "HOT.md"
    hot.parent.mkdir(parents=True, exist_ok=True)
    hot.write_text("# HOT\n\n- 新阶段\n", encoding="utf-8")
    third = agy_rules.sync_agy_rules(cfg)
    assert third["changed"] is True
    assert "新阶段" in target.read_text(encoding="utf-8")


def test_diary_status_respects_gate_for_private(
    stage2_env: tuple[AppConfig, Path, str]
) -> None:
    """gate 开启且调用方不是 casual 时，私账连条数都不该暴露。"""
    from agentbrain.server import BrokerService

    cfg, _, _ = stage2_env
    append_diary(cfg, ledger="private", content="私账内容", author="hermes:lily")

    svc = BrokerService(cfg)
    open_status = svc.diary_status(can_casual=False)
    assert "private" in open_status, "gate 关闭时应当能看到私账条数"

    mem_raw = yaml.safe_load(cfg.memory_config_path.read_text(encoding="utf-8"))
    mem_raw["diary"]["gate"] = True
    cfg.memory_config_path.write_text(yaml.safe_dump(mem_raw, allow_unicode=True), encoding="utf-8")
    gated = BrokerService(load_config(broker_path=cfg.broker_config_path, profile="lily"))

    assert "private" not in gated.diary_status(can_casual=False)
    assert "private" in gated.diary_status(can_casual=True)


def test_service_write_never_touches_real_home_gemini(
    stage2_env: tuple[AppConfig, Path, str]
) -> None:
    """回归：服务级写操作只能刷配置指向的 agy 文件，绝不能写用户的 ~/.gemini。

    真事故事实：临时配置的实例把真实 GEMINI.md 覆盖成了测试夹具（身份只剩 "Lily"），
    另一端因此"不像我"，且热记忆显示未配置。
    """
    from agentbrain.server import BrokerService
    from agentbrain.acl import AuthIdentity

    cfg, _, _ = stage2_env
    real = Path.home() / ".gemini" / "GEMINI.md"
    before = real.stat().st_mtime_ns if real.exists() else None

    svc = BrokerService(cfg)
    svc.write_note(
        {"content": "一条测试笔记", "type": "fact", "title": "测试写入"},
        identity=AuthIdentity(client_id="hermes:lily", domain="work", can_casual=True),
    )

    configured = cfg.broker.agy_rules_path
    assert configured is not None and configured.exists(), "配置指向的 agy 文件应被刷新"

    after = real.stat().st_mtime_ns if real.exists() else None
    assert after == before, "测试用例写到了真实的 ~/.gemini/GEMINI.md"


def test_agy_rules_carry_identity_without_diary_noise(
    stage2_env: tuple[AppConfig, Path, str]
) -> None:
    """agy 那份文件必须带「我是谁」的自我陈述，且不再注入今日日记计数与习惯催促。"""
    from agentbrain import agy_rules

    cfg, _, _ = stage2_env
    soul = cfg.broker.soul_path
    soul.write_text(
        "# SOUL\n你是 Testy，运行于Hermes Agent框架上的搭档。\n\n"
        "你不是工具，是搭档。\n## 人格\n### 特质\n- 说话干脆\n### 语气与表达规范\n- 默认中文\n",
        encoding="utf-8",
    )
    append_diary(cfg, ledger="public", content="今天聊了会儿", author="antigravity")

    text = agy_rules.build_agy_rules(cfg, target="antigravity")
    assert "你是 Testy" in text, "身份块丢了自我陈述，只剩特质清单"
    assert "Hermes Agent框架" not in text, "框架名没按端改写"
    assert "Antigravity CLI" in text
    assert "## 今日日记" not in text
    assert "公账 1 条" not in text
    assert "今天聊了会儿" not in text, "日记正文不该进规则文件"


def test_agy_rules_has_no_diary_nudge_when_empty(stage2_env: tuple[AppConfig, Path, str]) -> None:
    """公账为空时也不应出现逐轮/每次会话催记日记的静态块。"""
    from agentbrain import agy_rules

    cfg, _, _ = stage2_env
    text = agy_rules.build_agy_rules(cfg, target="antigravity")
    assert "## 今日日记" not in text
    assert "公账 0 条" not in text
    assert "今天还没有一条" not in text


def test_diary_append_does_not_leak_diary_status_to_agy_rules(stage2_env: tuple[AppConfig, Path, str]) -> None:
    """写完日记后，agy 规则文件不得包含日记正文或今日日记计数块。"""
    from agentbrain.server import BrokerService
    from agentbrain.acl import AuthIdentity

    cfg, _, _ = stage2_env
    target = Path(cfg.broker.agy_rules_path)
    ident = AuthIdentity(client_id="antigravity", domain="work")

    BrokerService(cfg).diary({"action": "append", "ledger": "public", "content": "第一条"}, identity=ident)
    text = target.read_text(encoding="utf-8")
    assert "## 今日日记" not in text
    assert "第一条" not in text, "日记正文不该进规则文件"


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


def test_hermes_display_source_decoupled_from_client_acl(
    stage2_env: tuple[AppConfig, Path, str]
) -> None:
    """Hermes 展示来源应归一化为 hermes，同时保留内部 client_id ACL 与 task_key。"""
    from agentbrain.acl import AuthIdentity, authenticate_bearer, hash_token
    from agentbrain.handoff import read_handoff
    from agentbrain.server import BrokerService

    cfg, tokens_path, _ = stage2_env
    hermes_token = "test-hermes-token-lily"
    tokens_map = json.loads(tokens_path.read_text(encoding="utf-8"))
    tokens_map[hash_token(hermes_token)] = "hermes:lily"
    tokens_path.write_text(json.dumps(tokens_map), encoding="utf-8")

    ident = authenticate_bearer(f"Bearer {hermes_token}", cfg, tokens_path=tokens_path)
    assert ident.client_id == "hermes:lily"
    assert ident.domain == "work"
    assert ident.can_casual is True
    assert ident.display_source == "hermes"

    svc = BrokerService(cfg, tokens_path=tokens_path)
    write_res = svc.write_note(
        {"content": "Hermes 写入的事实。", "type": "fact", "title": "Hermes 来源测试"},
        identity=ident,
    )
    note_text = (cfg.profile_dir / write_res["path"]).read_text(encoding="utf-8")
    assert "source: hermes\n" in note_text
    assert "source: hermes:lily" not in note_text

    svc.diary({"action": "append", "ledger": "public", "content": "公账记录"}, identity=ident)
    pub_status = svc.diary_status(can_casual=ident.can_casual)["public"]
    assert pub_status["last_author"] == "hermes"

    task_key = "hermes:local-session-123"
    svc.handoff(
        {
            "action": "upsert",
            "task_key": task_key,
            "status": "open",
            "summary": "保持 task_key 不被破坏",
        },
        identity=ident,
    )
    tasks = read_handoff(cfg)["tasks"]
    assert task_key in tasks
    assert tasks[task_key]["task_key"] == task_key
    assert tasks[task_key]["updated_by"] == "hermes"

    # 非 Hermes 客户端来源名保持原样
    ag_ident = AuthIdentity(client_id="antigravity", domain="work", can_casual=False)
    ag_res = svc.write_note(
        {"content": "Antigravity 写入的事实。", "type": "fact", "title": "AG 来源测试"},
        identity=ag_ident,
    )
    ag_text = (cfg.profile_dir / ag_res["path"]).read_text(encoding="utf-8")
    assert "source: antigravity\n" in ag_text

    codex_ident = AuthIdentity(client_id="codex", domain="work", can_casual=False)
    svc.diary({"action": "append", "ledger": "public", "content": "Codex 公账"}, identity=codex_ident)
    # 日记署名只允许 hermes / antigravity；其它来源统一归到 hermes。
    assert svc.diary_status(can_casual=False)["public"]["last_author"] == "hermes"
    svc.handoff(
        {"action": "upsert", "task_key": "codex:t1", "status": "open", "summary": "s"},
        identity=codex_ident,
    )
    assert read_handoff(cfg)["tasks"]["codex:t1"]["updated_by"] == "codex"


def test_write_note_rejects_removed_or_unknown_type_without_mkdir(
    stage2_env: tuple[AppConfig, Path, str]
) -> None:
    """已移除的 inbox/external-card 或未知 type 必须显式拒绝，且不得创建任何目录或文件。"""
    from agentbrain.acl import AuthIdentity
    from agentbrain.server import BrokerService, ServiceError

    cfg, tokens_path, _ = stage2_env
    svc = BrokerService(cfg, tokens_path=tokens_path)
    ident = AuthIdentity(client_id="hermes:lily", domain="work", can_casual=True)

    for bad_type in ("inbox", "external-card", "unknown_type"):
        with pytest.raises(ServiceError) as exc_info:
            svc.write_note(
                {"content": "不应落地的内容", "type": bad_type, "title": f"bad-{bad_type}"},
                identity=ident,
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.error == "invalid_request"

    assert not (cfg.profile_dir / "50_Inbox").exists()
    assert not (cfg.profile_dir / "50_Inbox" / "new").exists()
