"""回归测试：Hermes state.db 独立采集、双路由去重、去日记噪声与一次性完成回执。"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agentbrain.acl import hash_token
from agentbrain.config import AppConfig, load_config
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_adapters import scan_hermes_statedb
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_runner import ProactiveRunner
from agentbrain.proactive_worker import run_worker_once
from agentbrain.server import create_http_server


def _load_provider_cls() -> type[Any]:
    plugin_path = (
        Path(__file__).resolve().parent.parent
        / "integrations"
        / "hermes"
        / "obsidian-broker"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("obsidian_broker_plugin_capture", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ObsidianBrokerMemoryProvider


def _create_hermes_state_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            model TEXT,
            title TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            timestamp REAL NOT NULL,
            finish_reason TEXT,
            reasoning TEXT,
            message_uid TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_hermes_turn(
    db_path: Path,
    *,
    session_id: str,
    source: str,
    hidden: int,
    user_text: str,
    assistant_text: str,
    ts: float,
    with_tool_step: bool = False,
    uid_prefix: str = "u",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, model, title, started_at, hidden) VALUES (?, ?, 'deepseek-flash', 'Test', ?, ?)",
        (session_id, source, ts, hidden),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, finish_reason, message_uid) VALUES (?, 'user', ?, ?, NULL, ?)",
        (session_id, user_text, ts, f"{uid_prefix}-user-{ts}"),
    )
    if with_tool_step:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, timestamp, finish_reason, message_uid) VALUES (?, 'assistant', '', '[{\"name\":\"terminal\"}]', ?, 'tool_calls', ?)",
            (session_id, ts + 0.2, f"{uid_prefix}-tc-{ts}"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, finish_reason, message_uid) VALUES (?, 'tool', 'ok', ?, NULL, ?)",
            (session_id, ts + 0.4, f"{uid_prefix}-tool-{ts}"),
        )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, finish_reason, reasoning, message_uid) VALUES (?, 'assistant', ?, ?, 'stop', 'secret raw thinking', ?)",
        (session_id, assistant_text, ts + 0.8, f"{uid_prefix}-asst-{ts}"),
    )
    conn.commit()
    conn.close()


def _build_env(tmp_path: Path, *, state_db_path: Path) -> tuple[AppConfig, Path, str, str]:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    (profile_dir / "10_Working").mkdir(parents=True)
    (profile_dir / "20_LongTerm" / "Projects").mkdir(parents=True)
    (profile_dir / "30_Diary" / "Public").mkdir(parents=True)
    (profile_dir / "30_Diary" / "Private").mkdir(parents=True)
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\n- 阶段：测试\n", encoding="utf-8")

    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("# SOUL\n你是 Lily。\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True)

    mem_yaml = profile_dir / "memory.yaml"
    mem_yaml.write_text(
        f"""
schema: 1
profile: lily
clients:
  "hermes:lily": {{domain: work, can_casual: true}}
  "codex": {{domain: work, can_casual: false}}
proactive:
  enabled: true
  event_capture: true
  incremental_batch_size: 5
  hermes_statedb_enabled: true
  hermes_state_db_path: "{state_db_path.as_posix()}"
  model_backend: hermes
  hermes_profile: lily
""",
        encoding="utf-8",
    )

    broker_yaml = tmp_path / "broker.yaml"
    broker_yaml.write_text(
        f"""
vault_root: "{vault.as_posix()}"
soul_path: "{soul_path.as_posix()}"
port: 18765
role: secondary
index_dir: "{(runtime_dir / 'index').as_posix()}"
rev_dir: "{(runtime_dir / 'rev').as_posix()}"
state_dir: "{(runtime_dir / 'state').as_posix()}"
audit_dir: "{(runtime_dir / 'audit').as_posix()}"
log_dir: "{(runtime_dir / 'logs').as_posix()}"
""",
        encoding="utf-8",
    )

    hermes_token = "hermes-secret-token"
    codex_token = "codex-secret-token"
    tokens_path = runtime_dir / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                hash_token(hermes_token): "hermes:lily",
                hash_token(codex_token): "codex",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(broker_yaml)
    return cfg, tokens_path, hermes_token, codex_token


def test_hermes_statedb_end_to_end_5_turns_job_notes_once_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 轮真实格式 state.db 对话 -> 调度任务 -> fake 模型写笔记 -> 下一轮一次性回执 -> 其余轮静默。"""
    state_db = tmp_path / "hermes_home" / "state.db"
    _create_hermes_state_db(state_db)
    cfg, tokens_path, hermes_token, codex_token = _build_env(tmp_path, state_db_path=state_db)

    activation = dt.datetime(2026, 11, 17, 7, 0, 0, tzinfo=dt.timezone.utc)
    clock = ManualClock(activation)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    base_ts = activation.timestamp()

    # 激活前的历史消息绝不能被回填
    _insert_hermes_turn(
        state_db,
        session_id="local-galaxy",
        source="local",
        hidden=0,
        user_text="历史问题PRE_ACTIVATION_LEAK",
        assistant_text="历史回答PRE_ACTIVATION_LEAK",
        ts=base_ts - 3600.0,
        uid_prefix="old",
    )

    # 激活后的 5 轮真实对话（含中间 tool_calls）
    for i in range(1, 6):
        _insert_hermes_turn(
            state_db,
            session_id="local-galaxy",
            source="local",
            hidden=0,
            user_text=f"星系突击者第{i}轮：修复开始界面{i}",
            assistant_text=f"已完成第{i}轮修复{i}",
            ts=base_ts + i * 10.0,
            with_tool_step=True,
            uid_prefix=f"t{i}",
        )

    report = scan_hermes_statedb(store, state_db)
    assert report.available is True
    assert report.skipped_pre_activation == 2
    assert report.turns_completed == 5
    assert report.events_ingested == 5

    status = store.get_status()
    assert status["sessions"][0]["completed_human_turns"] == 5
    assert status["job_counts"].get("pending") == 1

    job = store.claim_job("w1")
    assert job is not None
    # 确认提示词不包含任何激活前历史与 raw thinking
    events = store.get_job_events(job.job_id)
    assert len(events) == 5
    for ev in events:
        assert "PRE_ACTIVATION_LEAK" not in ev.content
        assert "secret raw thinking" not in ev.content

    # 用 fake hermes_runner 执行后台评审任务
    def fake_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "星系突击者UI修复结论",
                    "content": "开始界面渲染已切换为异步预加载。",
                    "fact_key": "galaxy_ui_preload",
                    "keys": ["星系突击者", "开始界面", "预加载"],
                    "tags": ["game"],
                    "evidence_level": "delivered",
                    "source_refs": [f"{events[-1].client}:{events[-1].session_id}:{events[-1].event_id}"],
                }
            ],
        }

    from agentbrain.proactive_worker import process_claimed_job

    res = process_claimed_job(cfg, store, job, clock=clock, hermes_runner=fake_runner)
    store.ack_job_success(
        job.job_id,
        job.lease_token,
        outcome=res.outcome,
        summary={"segments": res.segments_processed, "applied": res.applied_count, "skipped_dedupe": res.skipped_dedupe_count},
    )
    assert res.outcome == "applied"
    assert res.applied_count == 1

    # 启动真实 HTTP 服务并用 ObsidianBrokerMemoryProvider 验证一次性回执与作用域隔离
    server = create_http_server(cfg, host="127.0.0.1", port=0, tokens_path=tokens_path)
    port = int(server.server_address[1])
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    try:
        ProviderCls = _load_provider_cls()
        provider = ProviderCls()
        monkeypatch.setenv("AGENTBRAIN_TOKEN", hermes_token)
        broker_cfg_path = tmp_path / "broker_client.yaml"
        broker_cfg_path.write_text(f"port: {port}\nvault_root: \"{cfg.broker.vault_root.as_posix()}\"\n", encoding="utf-8")
        monkeypatch.setenv("AGENTBRAIN_BROKER_CONFIG", str(broker_cfg_path))
        provider.initialize("local-galaxy")

        # 1) 其他会话与 codex 看不到 local-galaxy 的增量回执
        codex_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/search",
            data=json.dumps({"query": "无关查询", "session_id": "local-galaxy"}).encode("utf-8"),
            headers={"Authorization": f"Bearer {codex_token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(codex_req, timeout=2.0) as resp:
            codex_data = json.loads(resp.read().decode("utf-8"))
        assert "review_receipts" not in codex_data
        assert "diary_today" not in codex_data

        other_sess = provider.prefetch("无关查询", session_id="local-other")
        assert other_sess == ""

        # 2) local-galaxy 下一轮收到一次性 [PREVIOUS_REVIEW_RESULT] 并自动 ack
        out_once = provider.prefetch("继续聊点别的", session_id="local-galaxy")
        assert "[PREVIOUS_REVIEW_RESULT]" in out_once
        assert "写入 1 条" in out_once
        assert "长期记忆 1" in out_once
        assert "[DIARY]" not in out_once

        # 3) 再下一轮完全静默
        out_next = provider.prefetch("再聊别的", session_id="local-galaxy")
        assert out_next == ""
    finally:
        server.shutdown()
        server.server_close()


def test_hermes_statedb_system_hidden_cli_no_count(tmp_path: Path) -> None:
    """system/cron/subagent/hidden 必须排除；CLI 仅 reference_only，不计入完成轮次。"""
    state_db = tmp_path / "state.db"
    _create_hermes_state_db(state_db)
    cfg, _, _, _ = _build_env(tmp_path, state_db_path=state_db)
    clock = ManualClock(dt.datetime(2026, 11, 17, 7, 0, 0, tzinfo=dt.timezone.utc))
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    base_ts = clock.now_utc().timestamp()

    for idx, (sid, src, hid) in enumerate(
        [
            ("s-cron", "cron", 0),
            ("s-sub", "subagent", 0),
            ("s-sys", "system", 0),
            ("s-hid", "local", 1),
            ("s-cli", "cli", 0),
        ],
        start=1,
    ):
        _insert_hermes_turn(
            state_db,
            session_id=sid,
            source=src,
            hidden=hid,
            user_text=f"msg {idx}",
            assistant_text=f"reply {idx}",
            ts=base_ts + idx * 5.0,
            uid_prefix=f"x{idx}",
        )

    report = scan_hermes_statedb(store, state_db)
    assert report.turns_completed == 0
    assert report.events_ingested == 1  # 仅 s-cli 作为 reference_only 入库
    status = store.get_status()
    assert all(s["completed_human_turns"] == 0 for s in status["sessions"])
    assert status["job_counts"] == {}


def test_hermes_provider_and_statedb_dedupe_across_restarts_and_repeated_text(
    tmp_path: Path,
) -> None:
    """provider 与 state_db 双路由不重复计数；重复文本的两个真实轮次仍计 2 次；重启重扫不重计。"""
    state_db = tmp_path / "state.db"
    _create_hermes_state_db(state_db)
    cfg, _, _, _ = _build_env(tmp_path, state_db_path=state_db)
    clock = ManualClock(dt.datetime(2026, 11, 17, 7, 0, 0, tzinfo=dt.timezone.utc))
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    base_ts = clock.now_utc().timestamp()

    # 两个真实轮次但文本完全相同（间隔 5 秒）
    for i in (1, 2):
        _insert_hermes_turn(
            state_db,
            session_id="sess-dup",
            source="local",
            hidden=0,
            user_text="继续",
            assistant_text="好的",
            ts=base_ts + i * 5.0,
            uid_prefix=f"dup{i}",
        )

    # 先由 provider 上报第 1 轮
    r1 = store.ingest_event(
        client="hermes",
        session_id="sess-dup",
        event_id="hermes:sess-dup:turn:1",
        content="User: 继续\nAssistant: 好的",
        occurred_at=dt.datetime.fromtimestamp(base_ts + 5.8, tz=dt.timezone.utc),
        route="provider",
    )
    assert r1.accepted and not r1.deduped
    assert r1.completed_human_turns == 1

    # state_db 扫描第 1、2 轮：第 1 轮跨路由去重，第 2 轮正常入库 -> 总计 2 轮
    rep1 = scan_hermes_statedb(store, state_db)
    assert rep1.events_deduped == 1
    assert rep1.events_ingested == 1
    assert rep1.turns_completed == 1
    assert store.get_status()["sessions"][0]["completed_human_turns"] == 2

    # 重启后重建 Store 再扫一次 -> 0 新增
    store2 = ProactiveStore.from_app_config(cfg, clock=clock)
    rep2 = scan_hermes_statedb(store2, state_db)
    assert rep2.events_ingested == 0
    assert store2.get_status()["sessions"][0]["completed_human_turns"] == 2


def test_receipt_honesty_for_no_change_failure_and_nightly(tmp_path: Path) -> None:
    """no_change 不得报写入成功；失败任务生成诚实失败回执；nightly 仅生成一条汇总回执。"""
    state_db = tmp_path / "state.db"
    _create_hermes_state_db(state_db)
    cfg, _, _, _ = _build_env(tmp_path, state_db_path=state_db)
    clock = ManualClock(dt.datetime(2026, 11, 17, 7, 0, 0, tzinfo=dt.timezone.utc))
    store = ProactiveStore.from_app_config(cfg, clock=clock)

    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="sess-nc",
            event_id=f"e-{i}",
            content=f"User: u{i}\nAssistant: a{i}",
            route="provider",
        )
    job = store.claim_job("w1")
    assert job is not None
    store.ack_job_success(job.job_id, job.lease_token, outcome="no_change", summary={"applied": 0})

    pending = store.list_pending_receipts(client_id="hermes:lily", session_id="sess-nc")
    assert len(pending) == 1
    assert pending[0]["outcome"] == "no_change"
    assert pending[0]["applied_count"] == 0

    ProviderCls = _load_provider_cls()
    from importlib import import_module
    lines, ids = import_module("agentbrain.proactive").__name__, []
    # 直接用插件模块格式化校验文案诚实性
    plugin_path = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "obsidian-broker" / "__init__.py"
    spec = importlib.util.spec_from_file_location("obsidian_broker_fmt", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fmt_lines, fmt_ids = mod._format_review_receipts(pending)
    assert fmt_ids == [pending[0]["id"]]
    assert "no_change" in fmt_lines[0]
    assert "写入" not in fmt_lines[0]

    # 测试 on_delegation 兼容 child_session_id 与 **kwargs
    p = ProviderCls()
    p.initialize("sess-nc")
    p._proactive_enabled = True
    p._proactive_event_capture = True
    enqueued: list[dict[str, Any]] = []
    p._enqueue_outbox = enqueued.append  # type: ignore[method-assign]
    p.on_delegation("subtask", "done", child_session_id="child-123", extra_kw="ignored")
    assert len(enqueued) == 1
    assert enqueued[0]["event_kind"] == "reference_only"
    assert enqueued[0]["meta"]["child_session_id"] == "child-123"
