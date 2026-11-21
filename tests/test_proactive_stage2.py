"""Stage 2 主动记忆整理端到端隔离测试（临时路径 + 本地假 HTTP 服务器，不触碰真实配置/Vault/网络）。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentbrain.cli import main as cli_main
from agentbrain.config import (
    BrokerConfig,
    MemoryConfig,
    ProactiveConfig,
    ProactiveConfigError,
    AppConfig,
    load_memory_config,
    validate_proactive_worker_config,
)
from agentbrain.diary import DiaryGateError, append_diary
from agentbrain.index import connect_db, init_db
from agentbrain.proactive import EVENT_KIND_REFERENCE_ONLY, ProactiveStore
from agentbrain.proactive_adapters import ingest_cli_reference_event, scan_agy_ide_roots
from agentbrain.proactive_apply import (
    BatchValidationError,
    ConflictError,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_redaction import redact_secrets
from agentbrain.proactive_runner import ProactiveRunner, export_review_prompts
from agentbrain.proactive_worker import (
    ContextBudgetExceededError,
    process_claimed_job,
    run_worker_once,
)
from agentbrain.server import create_http_server


def _make_app_config(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
    diary_gate: bool = False,
    hot_max_chars: int = 1500,
) -> AppConfig:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    for sub in (
        "00_Inbox",
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
    soul.write_text("# Lily\n", encoding="utf-8")
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\ninitial\n", encoding="utf-8")

    broker_yaml = tmp_path / "broker.yaml"
    mem_yaml = profile_dir / "memory.yaml"
    broker_yaml.write_text(
        "\n".join([
            f"vault_root: {vault}",
            f"soul_path: {soul}",
            "port: 18799",
            "role: primary",
            f"index_dir: {tmp_path / 'runtime' / 'index'}",
            f"rev_dir: {tmp_path / 'runtime' / 'rev'}",
            f"state_dir: {tmp_path / 'runtime' / 'state'}",
            f"audit_dir: {tmp_path / 'runtime' / 'audit'}",
            f"log_dir: {tmp_path / 'runtime' / 'log'}",
            f"agy_rules_path: {tmp_path / 'GEMINI.md'}",
        ]) + "\n",
        encoding="utf-8",
    )
    mem_yaml.write_text("schema: 1\nprofile: lily\n", encoding="utf-8")

    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18799,
        role="primary",
        index_dir=tmp_path / "runtime" / "index",
        rev_dir=tmp_path / "runtime" / "rev",
        state_dir=tmp_path / "runtime" / "state",
        audit_dir=tmp_path / "runtime" / "audit",
        log_dir=tmp_path / "runtime" / "log",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    memory = MemoryConfig(
        profile="lily",
        clients={"hermes": {"domain": "work"}},
        hot_max_chars=hot_max_chars,
        diary_gate=diary_gate,
        proactive=proactive or ProactiveConfig(),
    )
    cfg = AppConfig(
        broker=broker,
        memory=memory,
        broker_config_path=broker_yaml,
        memory_config_path=mem_yaml,
    )
    init_db(cfg)
    return cfg


class _FakeModelServer:
    """本地假 OpenAI 兼容 HTTP 服务器（仅用于隔离测试）。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self._idx = 0
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                })
                if outer._idx < len(outer.responses):
                    resp_obj = outer.responses[outer._idx]
                    outer._idx += 1
                else:
                    resp_obj = {"outcome": "no_change", "operations": []}
                payload = {
                    "id": "chatcmpl-fake",
                    "choices": [{"message": {"role": "assistant", "content": json.dumps(resp_obj, ensure_ascii=False)}}],
                }
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _FakeModelServer:
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def test_config_defaults_and_fail_closed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    pdir.mkdir(parents=True)
    mem_yaml = pdir / "memory.yaml"
    mem_yaml.write_text("schema: 1\nprofile: lily\n", encoding="utf-8")

    mem, _ = load_memory_config(vault, "lily")
    assert mem.proactive.enabled is False
    assert mem.proactive.event_capture is False
    assert mem.proactive.agy_ide_enabled is False

    # 畸形 enabled 非布尔值 -> fail-closed
    mem_yaml.write_text("schema: 1\nproactive:\n  enabled: 'yes_please'\n", encoding="utf-8")
    with pytest.raises(ProactiveConfigError):
        load_memory_config(vault, "lily")

    # 直接把密钥写进 model_api_key_env -> fail-closed
    mem_yaml.write_text(
        "schema: 1\nproactive:\n  enabled: true\n  model_api_key_env: sk-secret12345678901234\n",
        encoding="utf-8",
    )
    with pytest.raises(ProactiveConfigError):
        load_memory_config(vault, "lily")

    # 启用但缺少 model_endpoint -> fail-closed
    with pytest.raises(ProactiveConfigError):
        validate_proactive_worker_config(ProactiveConfig(enabled=True, model_endpoint="", model_name="gpt-test"))


def test_hermes_provider_outbox_retry_identical_turns_and_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_plugin_env = os.environ.get("AGENTBRAIN_PLUGIN_PATH", "").strip()
    plugin_path = (
        Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "agentbrain" / "__init__.py"
        if raw_plugin_env == "source"
        else Path(raw_plugin_env or "__missing__")
    )
    if not plugin_path.exists():
        pytest.skip("AGENTBRAIN_PLUGIN_PATH not set")
    spec = importlib.util.spec_from_file_location("agentbrain_plugin_plugin_s2", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    pdir.mkdir(parents=True)
    (pdir / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\ndiary:\n  turn_capture: false\nproactive:\n  enabled: true\n  event_capture: true\n",
        encoding="utf-8",
    )
    broker_yaml = tmp_path / "broker.yaml"
    broker_yaml.write_text(f"port: 18799\nvault_root: {vault}\ntoken: tok-test\n", encoding="utf-8")
    monkeypatch.setenv("AGENTBRAIN_BROKER_CONFIG", str(broker_yaml))

    hermes_home = tmp_path / "hermes" / "lily"
    hermes_home.mkdir(parents=True)

    provider = mod.ObsidianBrokerMemoryProvider()
    provider.initialize("chat-1", hermes_home=str(hermes_home))

    posted: list[tuple[str, dict[str, Any]]] = []
    fail_once = {"fail": True}

    def fake_http(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if fail_once["fail"]:
            raise OSError("broker temporarily down")
        posted.append((path, payload or {}))
        return {"ok": True}

    monkeypatch.setattr(provider, "_http_json", fake_http)

    # 1. 两次完全相同的短句轮次：第一次网络失败留在 outbox；去除 <think> 与注入块
    provider.sync_turn(
        "[MEMORY_PREFETCH]\n- fact: old\n\n继续",
        "<think>secret reasoning</think>好的，继续。",
        session_id="chat-1",
        messages=[{"role": "user", "content": "继续"}, {"role": "assistant", "content": "好的"}],
    )
    # 等待后台线程写完 outbox 并尝试发送
    import time
    time.sleep(0.15)
    outbox_file = hermes_home / "cache" / "proactive_outbox.jsonl"
    assert outbox_file.exists() and len(outbox_file.read_text(encoding="utf-8").splitlines()) == 1

    # 2. 系统/bot/评审后台轮次被过滤
    provider.sync_turn("heartbeat", "ok", session_id="proactive-review-bg")
    provider.sync_turn("sys", "ok", session_id="chat-1", turn_author={"role": "system", "is_bot": True})

    # 3. 恢复网络后，第二次完全相同的轮次触发 outbox 重试并全部投递
    fail_once["fail"] = False
    provider.sync_turn(
        "继续",
        "好的，继续。",
        session_id="chat-1",
        messages=[
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "好的"},
        ],
    )
    provider.on_delegation("subtask", "done", session_id="chat-1")
    time.sleep(0.25)

    assert len(posted) == 3
    review_events = [p[1] for p in posted if p[0] == "/v1/review/events"]
    assert len(review_events) == 3
    human_ids = [e["event_id"] for e in review_events if e["event_kind"] == "human_turn"]
    assert len(human_ids) == 2 and human_ids[0] != human_ids[1]
    assert all("<think>" not in e["content"] and "[MEMORY_PREFETCH]" not in e["content"] for e in review_events)
    assert any(e["event_kind"] == "reference_only" for e in review_events)
    assert outbox_file.read_text(encoding="utf-8").strip() == ""


def test_agy_ide_adapter_fixture_partial_truncation_baseline_and_cli_ref(tmp_path: Path) -> None:
    cfg = _make_app_config(tmp_path)
    store = ProactiveStore.from_app_config(cfg)

    brain_root = tmp_path / "brain"
    conv1 = brain_root / "conv-1" / ".system_generated" / "logs"
    conv1.mkdir(parents=True)
    conv2_pb_only = brain_root / "conv-pb-only"
    conv2_pb_only.mkdir(parents=True)
    (conv2_pb_only / "state.pb").write_bytes(b"\x00\x01protobuf")

    t_file = conv1 / "transcript.jsonl"
    # 初始历史（baseline 应尾随跳过，不追溯上传）
    hist_lines = [
        json.dumps({
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-16T10:00:00Z",
            "content": "old question",
        }),
        json.dumps({
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-16T10:00:05Z",
            "content": "old answer",
        }),
    ]
    t_file.write_text("\n".join(hist_lines) + "\n", encoding="utf-8")

    r1 = scan_agy_ide_roots(store, (brain_root,), catchup=False)
    assert r1.events_ingested == 0
    assert r1.turns_completed == 0
    assert {"conv_id": "conv-pb-only", "reason": "source_unavailable_no_jsonl"} in r1.unavailable_sources

    # 追加新轮次：包含 USER_INPUT + tool_calls + GENERIC + 半截未写完的 JSON 行
    new_lines = [
        json.dumps({
            "step_index": 2,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-17T01:00:00Z",
            "content": "check tests",
            "thinking": "should not capture",
        }),
        json.dumps({
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-17T01:00:02Z",
            "content": "",
            "tool_calls": [{"name": "run_command"}],
        }),
        json.dumps({
            "step_index": 4,
            "source": "GENERIC",
            "type": "TOOL_RESULT",
            "status": "DONE",
            "created_at": "2026-11-17T01:00:03Z",
            "content": "85 passed",
        }),
    ]
    partial_line = '{"step_index": 5, "source": "MODEL"'
    with t_file.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(new_lines) + "\n" + partial_line)

    r2 = scan_agy_ide_roots(store, (brain_root,), catchup=False)
    assert r2.turns_completed == 0
    assert r2.events_ingested == 2  # 2 reference_only tool steps

    # 模拟文件重写截断（只留 step 5 完成行）：不能丢掉 pending_user_turn (step 2)
    final_line = json.dumps({
        "step_index": 5,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-11-17T01:00:06Z",
        "content": "All 85 tests passed.",
        "thinking": "hidden model thought",
    })
    t_file.write_text(final_line + "\n", encoding="utf-8")

    r3 = scan_agy_ide_roots(store, (brain_root,), catchup=False)
    assert r3.turns_completed == 1
    assert store.get_status()["sessions"][0]["completed_human_turns"] == 1

    # CLI reference capture 不计入人类轮次
    cli_res = ingest_cli_reference_event(store, session_id="cli-1", content="ran pytest")
    assert cli_res["completed_human_turns"] == 0


def test_fake_http_worker_no_change_and_disabled_no_network(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    with _FakeModelServer([{"outcome": "no_change", "operations": []}]) as srv:
        # 1. enabled=False 时绝不发起任何网络请求
        cfg_off = _make_app_config(
            tmp_path / "off",
            proactive=ProactiveConfig(enabled=False, model_endpoint=srv.endpoint, model_name="fake-gpt"),
        )
        store_off = ProactiveStore.from_app_config(cfg_off, clock=clock)
        for i in range(1, 6):
            store_off.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"turn {i}")
        assert run_worker_once(cfg_off, store_off, clock=clock) is None
        assert len(srv.requests) == 0

        # 2. enabled=True + no_change -> 0 文件写入，任务确认 no_change
        cfg_on = _make_app_config(
            tmp_path / "on",
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv.endpoint,
                model_name="fake-gpt",
                model_api_key_env="FAKE_PROACTIVE_KEY",
            ),
        )
        os.environ["FAKE_PROACTIVE_KEY"] = "test-key-local"
        try:
            store_on = ProactiveStore.from_app_config(cfg_on, clock=clock)
            hot_before = (cfg_on.profile_dir / "10_Working" / "HOT.md").read_text(encoding="utf-8")
            for i in range(1, 6):
                store_on.ingest_event(
                    client="hermes",
                    session_id="s1",
                    event_id=f"e-{i}",
                    content=f"hello bearer Bearer secret_token_1234567890 {i}",
                )
            res = run_worker_once(cfg_on, store_on, clock=clock)
            assert res is not None and res.outcome == "no_change" and res.applied_count == 0
            assert (cfg_on.profile_dir / "10_Working" / "HOT.md").read_text(encoding="utf-8") == hot_before
            # 验证凭证在发送给模型前已脱敏
            sent_user = srv.requests[0]["body"]["messages"][1]["content"]
            assert "secret_token_1234567890" not in sent_user
            assert "[REDACTED_TOKEN]" in sent_user
        finally:
            os.environ.pop("FAKE_PROACTIVE_KEY", None)


def test_restricted_projection_security_manual_blocks_conflict_and_diary_idempotency(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_app_config(
        tmp_path,
        proactive=ProactiveConfig(enabled=True, model_endpoint="http://127.0.0.1:1/v1", model_name="fake"),
        hot_max_chars=120,
    )
    proj_path = cfg.profile_dir / "20_LongTerm" / "Projects" / "Alpha.md"
    proj_path.write_text(
        "# Alpha\n\nManual notes stay here.\n\n<!-- agentbrain:managed:start -->\nold state\n<!-- agentbrain:managed:end -->\n\nFooter manual.\n",
        encoding="utf-8",
    )
    proj_hash = sha256_text(proj_path.read_text(encoding="utf-8"))

    store = ProactiveStore.from_app_config(cfg, clock=clock)
    ev_time = dt.datetime(2026, 11, 10, 14, 0, tzinfo=dt.timezone.utc)
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="s-proj",
            event_id=f"p-{i}",
            content=f"progress {i}",
            occurred_at=ev_time,
        )
    job = store.claim_job("w1")
    assert job is not None
    events = store.get_job_events(job.job_id)

    # 1. 整批原子校验：含非法路径 / 越界 HOT 必须拒绝整批（零写入）
    with pytest.raises(BatchValidationError):
        validate_and_apply_batch(
            cfg,
            store,
            job,
            events,
            {
                "outcome": "operations",
                "operations": [
                    {"op_type": "longterm_upsert", "note_type": "fact", "title": "Good", "content": "ok"},
                    {"op_type": "hot_update", "content": "H" * 200},
                ],
            },
            clock=clock,
        )
    assert not (cfg.profile_dir / "20_LongTerm" / "Facts" / "Good.md").exists()

    for bad_path in ("../../SOUL.md", "30_Diary/Public/2026-11-10.md", "SOUL.md", "memory.yaml"):
        with pytest.raises(BatchValidationError):
            validate_and_apply_batch(
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
                            "title": "Bad",
                            "content": "x",
                            "target_path": bad_path,
                        }
                    ],
                },
                clock=clock,
            )

    # 2. 冲突检测：expected_hash 不匹配拒绝覆盖
    with pytest.raises(ConflictError):
        validate_and_apply_batch(
            cfg,
            store,
            job,
            events,
            {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "project_update",
                        "project": "Alpha",
                        "managed_content": "new state",
                        "source_refs": ["p-1"],
                        "expected_hash": "deadbeef",
                    }
                ],
            },
            clock=clock,
        )

    # 3. 未提供真实工具验证证据时，verified 自动降级为 report；受管块保留手工段落并生成 _Overview.md；
    #    日记日期基于事件时间 (2026-11-10) 而非当前时钟 (2026-11-18)
    # 先模拟崩溃部分写：日记文件已写入但 proactive_applied_ops 尚未记录
    append_diary(cfg, ledger="public", content="Alpha milestone reached", author="proactive", now=dt.datetime(2026, 11, 10, 22, 0))

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
                    "title": "Alpha Fact",
                    "content": "Alpha uses SQLite.",
                    "fact_key": "alpha.db",
                    "evidence_level": "verified",
                    "source_refs": ["p-1"],
                },
                {
                    "op_type": "project_update",
                    "project": "Alpha",
                    "managed_content": "status: active\nmilestone: stage2",
                    "source_refs": ["p-1"],
                    "expected_hash": proj_hash,
                },
                {
                    "op_type": "diary_append",
                    "ledger": "public",
                    "content": "Alpha milestone reached",
                    "source_refs": ["p-1"],
                },
            ],
        },
        clock=clock,
    )
    assert summary.applied_count == 2
    assert summary.skipped_dedupe_count == 1  # 崩溃重放日记未重复追加！

    alpha_files = sorted((cfg.profile_dir / "20_LongTerm" / "Facts").glob("FACT-*-Alpha-Fact.md"))
    assert len(alpha_files) == 1
    fact_text = alpha_files[0].read_text(encoding="utf-8")
    assert "evidence_level: report" in fact_text

    updated_proj = proj_path.read_text(encoding="utf-8")
    assert "Manual notes stay here." in updated_proj
    assert "Footer manual." in updated_proj
    assert "milestone: stage2" in updated_proj
    overview = (cfg.profile_dir / "20_LongTerm" / "Projects" / "_Overview.md").read_text(encoding="utf-8")
    # 总览是薄索引（链接 + 一句话摘要），不再整段复制受管块
    # 已有旧式项目页（Alpha.md）会被沿用，总览里是「链接 + 一句话摘要」的薄索引
    assert "[[Alpha]]" in overview and "Manual notes stay here." not in overview

    diary_10 = cfg.profile_dir / "30_Diary" / "Public" / "2026-11-10.md"
    assert diary_10.exists() and diary_10.read_text(encoding="utf-8").count("Alpha milestone reached") == 1
    assert not (cfg.profile_dir / "30_Diary" / "Public" / "2026-11-18.md").exists()

    # 日记绝不进入检索索引
    with connect_db(cfg.broker.db_path) as conn:
        indexed_paths = [str(r["path"]) for r in conn.execute("SELECT path FROM notes").fetchall()]
    assert not any("30_Diary" in p for p in indexed_paths)


def test_budget_segmentation_lease_concurrency_and_http_endpoints(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    # 1. 上下文预算分段与单条超限显式失败
    with _FakeModelServer([
        {"outcome": "no_change", "operations": []},
        {"outcome": "no_change", "operations": []},
    ]) as srv:
        cfg = _make_app_config(
            tmp_path,
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv.endpoint,
                model_name="fake-gpt",
                model_max_context_chars=2400,
                max_payload_chars=1000,
            ),
        )
        store = ProactiveStore.from_app_config(cfg, clock=clock)
        for i in range(1, 6):
            store.ingest_event(client="hermes", session_id="seg", event_id=f"seg-{i}", content="M" * 450)
        res = run_worker_once(cfg, store, clock=clock)
        assert res is not None and res.segments_processed >= 2

        # 单条超限必须显式失败，绝不静默丢弃
        cfg_tiny = _make_app_config(
            tmp_path / "tiny",
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv.endpoint,
                model_name="fake-gpt",
                model_max_context_chars=6000,
                max_payload_chars=1500,
            ),
        )
        store_tiny = ProactiveStore.from_app_config(cfg_tiny, clock=clock)
        for i in range(1, 6):
            store_tiny.ingest_event(client="hermes", session_id="tiny", event_id=f"t-{i}", content="B" * 1200)
        job_tiny = store_tiny.claim_job("w1")
        assert job_tiny is not None
        # 需求 1：单条事件超单段预算时确定性切分并保留全部证据，不再整单硬失败
        res_tiny = process_claimed_job(cfg_tiny, store_tiny, job_tiny, clock=clock)
        assert res_tiny is not None
        assert res_tiny.segments_processed >= 2

    # 2. 进程级耐久租约互斥
    tok1 = store.try_acquire_runner_lease("proc-1", lease_seconds=60)
    assert tok1 is not None
    assert store.try_acquire_runner_lease("proc-2", lease_seconds=60) is None
    store.release_runner_lease("proc-1", tok1)
    assert store.try_acquire_runner_lease("proc-2", lease_seconds=60) is not None

    # 3. HTTP 接口（鉴权、413 超限、脱敏状态）
    from agentbrain.acl import hash_token

    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(json.dumps({hash_token("tok-hermes"): "hermes"}), encoding="utf-8")
    httpd = create_http_server(cfg, host="127.0.0.1", port=0, tokens_path=tokens_file)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/review/events",
            data=json.dumps({
                "client": "hermes",
                "session_id": "http-s1",
                "event_id": "h-1",
                "content": "TOP_SECRET_RAW_CHAT_TEXT",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer tok-hermes"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200

        # 超限事件必须返回 413，绝不静默截断并返回 200
        big_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/review/events",
            data=json.dumps({
                "client": "hermes",
                "session_id": "http-s1",
                "event_id": "h-big",
                "content": "Z" * (1000 * 16 + 100),
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer tok-hermes"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(big_req, timeout=3)
        assert exc_info.value.code == 413

        st_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/review/status",
            headers={"Authorization": "Bearer tok-hermes"},
            method="GET",
        )
        with urllib.request.urlopen(st_req, timeout=3) as resp:
            body = resp.read().decode("utf-8")
            assert "TOP_SECRET_RAW_CHAT_TEXT" not in body

        # 导出提示词 dry-run
        out_dir = tmp_path / "exported"
        files = export_review_prompts(cfg, store, out_dir)
        assert isinstance(files, list)
    finally:
        httpd.shutdown()
        httpd.server_close()

    # 4. CLI 诊断子命令
    assert cli_main(["--config", str(cfg.broker_config_path), "review-status"]) == 0
    assert cli_main(["--config", str(cfg.broker_config_path), "review-tick"]) == 0
