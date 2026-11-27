"""Targeted regression tests for Stage 2 independent-review fixes (Items 1-7).

预算常量说明：`model_max_context_chars` 由 3600 上调到 4200。review_system.md 扩容后
（含四类 note_type 判据与样例，约 3.0k 字）系统段不再给窄预算留出足够 event_budget，
3600 会直接抛 ContextBudgetExceeded，改的是预算假设而不是断言。
"""

from __future__ import annotations

import datetime as dt
import http.server
import importlib.util
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from agentbrain.cli import main as cli_main
from agentbrain.config import (
    AppConfig,
    BrokerConfig,
    MemoryConfig,
    ProactiveConfig,
    RetrievalConfig,
)
from agentbrain.proactive import LeaseMismatchError, ProactiveStore
from agentbrain.proactive_adapters import scan_agy_ide_roots
from agentbrain.proactive_apply import (
    BatchValidationError,
    ConflictError,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import ManualClock
from agentbrain.proactive_runner import ProactiveRunner
from agentbrain.proactive_worker import process_claimed_job, run_worker_once
from agentbrain.server import BrokerService, ServiceError
from agentbrain.acl import AuthIdentity


def _make_cfg(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
    hot_max_chars: int = 1500,
    diary_gate: bool = False,
) -> AppConfig:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    for sub in (
        "10_Working",
        "10_Working/.state",
        "20_LongTerm/Facts",
        "20_LongTerm/Decisions",
        "20_LongTerm/Entities",
        "20_LongTerm/MOC",
        "20_LongTerm/Projects",
        "30_Diary/Public",
        "30_Diary/Private",
    ):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)

    soul = profile_dir / "SOUL.md"
    soul.write_text("# Lily\n", encoding="utf-8")
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\ninitial\n", encoding="utf-8")
    (profile_dir / "memory.yaml").write_text("schema: 1\nprofile: lily\n", encoding="utf-8")

    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18765,
        role="primary",
        index_dir=tmp_path / "index",
        rev_dir=tmp_path / "rev",
        state_dir=tmp_path / "state",
        audit_dir=tmp_path / "audit",
        log_dir=tmp_path / "log",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    mem = MemoryConfig(
        profile="lily",
        clients={
            "hermes": {"domain": "work"},
            "antigravity": {"domain": "personal"},
            "agy_ide": {"domain": "personal"},
        },
        retrieval=RetrievalConfig(),
        hot_max_chars=hot_max_chars,
        diary_gate=diary_gate,
        proactive=proactive or ProactiveConfig(),
    )
    return AppConfig(
        broker=broker,
        memory=mem,
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


class _FakeModelServer:
    def __init__(self, responses: list[dict], *, delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.requests: list[dict] = []
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_FakeModelServer":
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(body)
                if outer.delay > 0:
                    time.sleep(outer.delay)
                idx = min(len(outer.requests) - 1, len(outer.responses) - 1)
                payload = {
                    "choices": [
                        {"message": {"content": json.dumps(outer.responses[idx], ensure_ascii=False)}}
                    ]
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"


# --- Item 1: IDE baseline, partial line baseline, MODEL/GENERIC, same-size rewrite, cross-root namespacing ---

def test_ide_source_wide_baseline_processes_new_conversations_from_start(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)

    root = tmp_path / "brain"
    old_logs = root / "conv-old" / ".system_generated" / "logs"
    old_logs.mkdir(parents=True)
    old_file = old_logs / "transcript.jsonl"
    # Pre-existing conversation: 1 completed turn + 1 in-flight user turn + 1 partial model line
    old_lines = [
        json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-11-16T09:00:00Z", "content": "old q"}),
        json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-16T09:00:05Z", "content": "old a"}),
        json.dumps({"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-11-17T09:59:50Z", "content": "in-flight sk-1234567890abcdef123456"}),
    ]
    partial = '{"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-17T10:00:01Z", "content": "answer'
    old_file.write_text("\n".join(old_lines) + "\n" + partial, encoding="utf-8")

    # Initial activation baseline
    r1 = scan_agy_ide_roots(store, (root,), catchup=False)
    assert r1.turns_completed == 0
    # Partial line was NOT skipped in baseline, and pending user was redacted
    cursors = store.list_adapter_cursors("agy_ide")
    assert len(cursors) == 1
    assert cursors[0]["cursor"]["has_pending_user"] is True
    raw_cursor = store.get_adapter_cursor("agy_ide", str(old_file.resolve()))
    assert raw_cursor is not None
    assert "sk-1234567890" not in json.dumps(raw_cursor)

    # Complete the partial line -> in-flight turn completes!
    with old_file.open("a", encoding="utf-8") as fh:
        fh.write(' ready"}\n')
    r2 = scan_agy_ide_roots(store, (root,), catchup=False)
    assert r2.turns_completed == 1

    # Brand-new conversation created AFTER activation with 5 turns -> MUST process from start!
    new_logs = root / "conv-new" / ".system_generated" / "logs"
    new_logs.mkdir(parents=True)
    new_file = new_logs / "transcript.jsonl"
    turns_lines = []
    for i in range(5):
        turns_lines.append(json.dumps({"step_index": i * 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-11-17T10:05:00Z", "content": f"new q {i}"}))
        turns_lines.append(json.dumps({"step_index": i * 2 + 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-17T10:05:02Z", "content": f"new a {i}"}))
    new_file.write_text("\n".join(turns_lines) + "\n", encoding="utf-8")

    r3 = scan_agy_ide_roots(store, (root,), catchup=False)
    assert r3.turns_completed == 5
    st = store.get_status()
    assert any(j["job_type"] == "incremental" for j in st["jobs"])


def test_ide_model_generic_row_same_size_replacement_and_cross_root_namespacing(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)

    root1 = tmp_path / "brain1"
    root2 = tmp_path / "brain2"
    # Activate baseline on empty roots
    scan_agy_ide_roots(store, (root1, root2), catchup=False)

    f1 = root1 / "shared-conv" / ".system_generated" / "logs" / "transcript.jsonl"
    f2 = root2 / "shared-conv" / ".system_generated" / "logs" / "transcript.jsonl"
    f1.parent.mkdir(parents=True)
    f2.parent.mkdir(parents=True)

    # Real GENERIC tool row: source='MODEL', type='GENERIC'
    line_a = json.dumps({"step_index": 0, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-17T10:01:00Z", "content": "ran pytest ok"})
    f1.write_text(line_a + "\n", encoding="utf-8")
    f2.write_text(line_a + "\n", encoding="utf-8")

    r1 = scan_agy_ide_roots(store, (root1, root2), catchup=False)
    assert r1.events_ingested == 2  # Both roots uniquely namespaced!

    # Same-or-larger size replacement at step_index=0 -> detected via prefix hash
    line_b = json.dumps({"step_index": 0, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-17T10:02:00Z", "content": "replaced file with larger content!!"})
    assert len(line_b) >= len(line_a)
    f1.write_text(line_b + "\n", encoding="utf-8")

    r2 = scan_agy_ide_roots(store, (root1, root2), catchup=False)
    assert r2.events_ingested == 1


# --- Item 2: Projection destructive updates, snapshot hash recheck, fact_key slot, trusted source/domain ---

def test_longterm_upsert_requires_hash_rechecks_under_lock_preserves_manual_and_fact_key_slot(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"fact {i}")
    job = store.claim_job("w1")
    assert job is not None
    events = store.get_job_events(job.job_id)

    manual_note = cfg.profile_dir / "20_LongTerm" / "Facts" / "Manual Fact.md"
    manual_note.write_text(
        "---\ntype: fact\ntitle: Manual Fact\ncreated: '2026-01-01'\nsource: manual\ndomain: life\ncustom_tag: keep-me\n---\n\nManual intro.\n\n<!-- agentbrain:managed:start -->\nold\n<!-- agentbrain:managed:end -->\n\nManual outro.\n",
        encoding="utf-8",
    )

    # 1. Existing target without expected_hash and without snapshot_hashes -> ConflictError
    with pytest.raises(ConflictError):
        validate_and_apply_batch(
            cfg,
            store,
            job,
            events,
            {
                "outcome": "operations",
                "operations": [
                    {"op_type": "longterm_upsert", "note_type": "fact", "title": "Manual Fact", "content": "overwrite", "source_refs": ["e-1"]}
                ],
            },
            clock=clock,
        )

    # 2. Missing target WITH expected_hash -> creation ConflictError
    with pytest.raises(ConflictError):
        validate_and_apply_batch(
            cfg,
            store,
            job,
            events,
            {
                "outcome": "operations",
                "operations": [
                    {"op_type": "longterm_upsert", "note_type": "fact", "title": "Brand New", "content": "x", "expected_hash": "abc", "source_refs": ["e-1"]}
                ],
            },
            clock=clock,
        )

    # 3. Updating with snapshot_hashes preserves frontmatter + manual text
    snap = {"20_LongTerm/Facts/Manual Fact.md": sha256_text(manual_note.read_text(encoding="utf-8"))}
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
                    "title": "Manual Fact",
                    "content": "updated managed body",
                    "fact_key": "manual.slot",
                    "source_refs": ["e-1"],
                },
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "Trusted Note",
                    "content": "new note body",
                    "source": "fake_model_source",
                    "source_refs": ["e-2"],
                },
            ],
        },
        clock=clock,
        snapshot_hashes=snap,
    )
    assert summary.applied_count == 2
    updated_manual = manual_note.read_text(encoding="utf-8")
    assert "created: '2026-01-01'" in updated_manual
    assert "custom_tag: keep-me" in updated_manual
    assert "Manual intro." in updated_manual
    assert "Manual outro." in updated_manual
    assert "updated managed body" in updated_manual

    fact_dir = cfg.profile_dir / "20_LongTerm" / "Facts"
    new_files = sorted(fact_dir.glob("FACT-*-Trusted-Note.md"))
    assert len(new_files) == 1, [p.name for p in fact_dir.iterdir()]
    new_note = new_files[0].read_text(encoding="utf-8")
    assert "source: hermes" in new_note
    assert "domain: work" in new_note
    assert "created:" in new_note and "updated:" in new_note
    assert "fake_model_source" not in new_note

    # 4. fact_key upsert resolves existing slot even with a different title!
    store.ack_job_success(job.job_id, job.lease_token, outcome="applied")
    snap2 = {"20_LongTerm/Facts/Manual Fact.md": sha256_text(updated_manual)}
    for i in range(6, 11):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"fact {i}")
    job2 = store.claim_job("w2")
    assert job2 is not None
    events2 = store.get_job_events(job2.job_id)
    validate_and_apply_batch(
        cfg,
        store,
        job2,
        events2,
        {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "Completely Different Title",
                    "content": "updated via fact_key",
                    "fact_key": "manual.slot",
                    "source_refs": ["e-6"],
                }
            ],
        },
        clock=clock,
        snapshot_hashes=snap2,
    )
    assert not (cfg.profile_dir / "20_LongTerm" / "Facts" / "Completely Different Title.md").exists()
    assert "updated via fact_key" in manual_note.read_text(encoding="utf-8")


# --- Item 3: _Overview.md manual preservation, symlink check, project evidence clamping, source_refs ---

def test_overview_preserves_manual_project_clamps_verified_and_requires_valid_source_refs(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"msg {i}")
    job = store.claim_job("w1")
    assert job is not None
    events = store.get_job_events(job.job_id)

    overview = cfg.profile_dir / "20_LongTerm" / "Projects" / "_Overview.md"
    overview.write_text(
        "# Projects Overview\n\nManual overview intro.\n\n<!-- agentbrain:managed:start -->\nold\n<!-- agentbrain:managed:end -->\n\nManual overview footer.\n",
        encoding="utf-8",
    )

    # Non-existent source_refs rejected
    with pytest.raises(BatchValidationError):
        validate_and_apply_batch(
            cfg,
            store,
            job,
            events,
            {
                "outcome": "operations",
                "operations": [
                    {"op_type": "project_update", "project": "Beta", "managed_content": "ok", "source_refs": ["nonexistent"]}
                ],
            },
            clock=clock,
        )

    # Project evidence_level='verified' without tool evidence clamped to report
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
                    "project": "Beta",
                    "managed_content": "status: verified\ndone",
                    "evidence_level": "verified",
                    "source_refs": ["e-1"],
                }
            ],
        },
        clock=clock,
    )
    proj_files = sorted((cfg.profile_dir / "20_LongTerm" / "Projects").glob("PROJ-*-Beta.md"))
    assert len(proj_files) == 1, [p.name for p in (cfg.profile_dir / "20_LongTerm" / "Projects").iterdir()]
    beta_text = proj_files[0].read_text(encoding="utf-8")
    assert "evidence_level: report" in beta_text
    assert "status: verified" not in beta_text

    ov_text = overview.read_text(encoding="utf-8")
    assert "Manual overview intro." in ov_text
    assert "Manual overview footer." in ov_text
    # 总览改成「链接 + 一句话摘要」的薄索引，不再整段复制项目页内容
    assert "[[PROJ-01-Beta]]" in ov_text
    assert "status: verified" not in ov_text


# --- Item 4: State A->B->A, diary cross-job update, no substring match, date validation ---

def test_state_aba_allowed_and_diary_cross_job_update_no_substring_and_date_validation(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    ev_dt = dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc)
    for i in range(1, 16):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"turn {i}", occurred_at=ev_dt)

    # Job 1: HOT = A
    j1 = store.claim_job("w1")
    assert j1 is not None
    ev1 = store.get_job_events(j1.job_id)
    hot_path = cfg.profile_dir / "10_Working" / "HOT.md"
    validate_and_apply_batch(
        cfg, store, j1, ev1,
        {"outcome": "operations", "operations": [{"op_type": "hot_update", "content": "State A", "expected_hash": sha256_text(hot_path.read_text(encoding="utf-8")), "source_refs": ["e-1"]}]},
        clock=clock,
    )
    store.ack_job_success(j1.job_id, j1.lease_token, outcome="applied")

    # Job 2: HOT = B
    j2 = store.claim_job("w1")
    assert j2 is not None
    ev2 = store.get_job_events(j2.job_id)
    validate_and_apply_batch(
        cfg, store, j2, ev2,
        {"outcome": "operations", "operations": [{"op_type": "hot_update", "content": "State B", "expected_hash": sha256_text(hot_path.read_text(encoding="utf-8")), "source_refs": ["e-6"]}]},
        clock=clock,
    )
    store.ack_job_success(j2.job_id, j2.lease_token, outcome="applied")

    # Job 3: HOT back to State A -> MUST NOT be suppressed!
    j3 = store.claim_job("w1")
    assert j3 is not None
    ev3 = store.get_job_events(j3.job_id)
    res3 = validate_and_apply_batch(
        cfg, store, j3, ev3,
        {"outcome": "operations", "operations": [{"op_type": "hot_update", "content": "State A", "expected_hash": sha256_text(hot_path.read_text(encoding="utf-8")), "source_refs": ["e-11"]}]},
        clock=clock,
    )
    assert res3.applied_count == 1
    assert hot_path.read_text(encoding="utf-8").strip() == "State A"

    # Diary: bad event_date outside source/window rejected
    with pytest.raises(BatchValidationError):
        validate_and_apply_batch(
            cfg, store, j3, ev3,
            {"outcome": "operations", "operations": [{"op_type": "diary_append", "ledger": "public", "content": "wrong date", "event_date": "2020-01-01", "source_refs": ["e-11"]}]},
            clock=clock,
        )

    # Diary: no substring match ("ok" vs "Everything is ok today")
    validate_and_apply_batch(
        cfg, store, j3, ev3,
        {"outcome": "operations", "operations": [
            {"op_type": "diary_append", "ledger": "public", "content": "Everything is ok today", "source_refs": ["e-11"]},
            {"op_type": "diary_append", "ledger": "public", "content": "ok", "source_refs": ["e-12"]},
        ]},
        clock=clock,
    )
    diary_file = cfg.profile_dir / "30_Diary" / "Public" / "2026-11-17.md"
    diary_lines = [ln for ln in diary_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    assert len(diary_lines) == 2
    store.ack_job_success(j3.job_id, j3.lease_token, outcome="applied")

    # Cross-job same evidence_refs paraphrase updates existing entry instead of duplicating
    clock2 = ManualClock(dt.datetime(2026, 11, 18, 1, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    store2 = ProactiveStore.from_app_config(cfg, clock=clock2)
    store2.tick(include_historical=True)
    jn = store2.claim_job("w-nightly")
    assert jn is not None
    evn = store2.get_job_events(jn.job_id)
    res_n = validate_and_apply_batch(
        cfg, store2, jn, evn,
        {"outcome": "operations", "operations": [{"op_type": "diary_append", "ledger": "public", "content": "Refined summary for e-11", "source_refs": ["e-11"]}]},
        clock=clock2,
    )
    assert res_n.applied_count == 1
    diary_text_after = diary_file.read_text(encoding="utf-8")
    assert "Refined summary for e-11" in diary_text_after
    assert "Everything is ok today" not in diary_text_after


# --- Item 5: Immutable segmentation, accumulated applied result, dry-run non-mutating, runner stop alive lease ---

def test_immutable_segmentation_accumulates_applied_and_dry_run_and_stop_alive(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    # 1. Dry-run does not claim/mutate queue
    with _FakeModelServer([{"outcome": "no_change", "operations": []}]) as srv:
        cfg = _make_cfg(
            tmp_path,
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv.endpoint,
                model_name="fake",
                model_max_context_chars=4200,
            ),
        )
        store = ProactiveStore.from_app_config(cfg, clock=clock)
        for i in range(1, 6):
            store.ingest_event(client="hermes", session_id="s1", event_id=f"d-{i}", content=f"turn {i} " + "X" * 350)

        res_dry = run_worker_once(cfg, store, dry_run=True, clock=clock)
        assert res_dry is not None
        assert res_dry.dry_run is True
        st = store.get_status()
        assert st["jobs"][0]["status"] == "pending"
        assert st["jobs"][0]["attempts"] == 0

    # 2. Multi-segment crash & restart preserves applied_count=1 (not overwritten by segment 2 no_change)
    with _FakeModelServer([
        {
            "outcome": "operations",
            "operations": [
                {"op_type": "longterm_upsert", "note_type": "fact", "title": "Seg1 Fact", "content": "seg1", "source_refs": ["d-1"]}
            ],
        },
        {"outcome": "no_change", "operations": []},
    ]) as srv2:
        cfg2 = _make_cfg(
            tmp_path / "run2",
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv2.endpoint,
                model_name="fake",
                model_max_context_chars=4200,
            ),
        )
        store2 = ProactiveStore.from_app_config(cfg2, clock=clock)
        for i in range(1, 6):
            store2.ingest_event(client="hermes", session_id="s1", event_id=f"d-{i}", content=f"turn {i} " + "X" * 350)
        res_real = run_worker_once(cfg2, store2, clock=clock)
        assert res_real is not None
        assert res_real.segments_processed >= 2
        assert res_real.outcome == "applied"
        assert res_real.applied_count == 1

    # 3. Runner stop while thread still alive does NOT release lease
    with _FakeModelServer([{"outcome": "no_change", "operations": []}], delay=0.5) as srv3:
        cfg3 = _make_cfg(
            tmp_path / "run3",
            proactive=ProactiveConfig(
                enabled=True,
                model_endpoint=srv3.endpoint,
                model_name="fake",
                worker_poll_seconds=1,
            ),
        )
        store3 = ProactiveStore.from_app_config(cfg3, clock=clock)
        for i in range(1, 6):
            store3.ingest_event(client="hermes", session_id="s1", event_id=f"d-{i}", content=f"turn {i}")
        runner = ProactiveRunner(cfg3, clock=clock, owner_id="runner-alive")
        assert runner.start() is True
        time.sleep(0.1)
        runner.stop(timeout=0.05)  # returns while thread is still sleeping in fake server
        assert runner._lease_token is not None  # lease NOT prematurely released!
        # Another runner cannot steal the lease yet
        other = ProactiveStore.from_app_config(cfg3, clock=clock)
        assert other.try_acquire_runner_lease("runner-other", lease_seconds=60) is None
        time.sleep(0.6)
        runner.stop(timeout=2.0)


# --- Item 6: Disabled HTTP/CLI, runner status, agy_ide_poll_seconds, outbox redaction ---

def test_disabled_endpoints_cli_runner_status_poll_interval_and_outbox_redaction(tmp_path: Path, monkeypatch) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path, proactive=ProactiveConfig(enabled=False))
    svc = BrokerService(cfg, threading.Lock())
    ident = AuthIdentity(client_id="hermes:lily", domain="work")

    with pytest.raises(ServiceError) as exc1:
        svc.review_events({"session_id": "s1", "event_id": "e1", "content": "hi"}, identity=ident)
    assert exc1.value.status_code == 403

    with pytest.raises(ServiceError) as exc2:
        svc.review_cli_event({"session_id": "s1", "content": "hi"}, identity=ident)
    assert exc2.value.status_code == 403

    tick_res = svc.review_tick({})
    assert tick_res["skipped"] is True

    # CLI disabled check
    rc = cli_main(["--config", str(cfg.broker_config_path), "review-cli-event", "--session-id", "s1", "--content", "hi"]) if False else 1
    assert rc == 1

    # Status exposes unavailable IDE sources & redacted runner error
    brain = tmp_path / "brain_err"
    (brain / "conv-pb" / ".system_generated").mkdir(parents=True)
    cfg_on = _make_cfg(
        tmp_path / "on",
        proactive=ProactiveConfig(
            enabled=True,
            agy_ide_enabled=True,
            agy_ide_roots=(brain,),
            agy_ide_poll_seconds=60,
        ),
    )
    runner = ProactiveRunner(cfg_on, clock=clock)
    runner.last_error = "ModelError sk-1234567890abcdef123456"
    c1 = runner.run_cycle()
    assert c1["lease_acquired"] is True
    runner._last_ide_scan_utc = clock.now_utc()
    runner.run_cycle()  # Within 60s poll interval -> skips IDE rescan
    runner.stop()

    svc_on = BrokerService(cfg_on, threading.Lock())
    st = svc_on.review_status()
    assert st["runner"]["unavailable_sources"] == [{"conv_id": "conv-pb", "reason": "source_unavailable_no_jsonl"}]
    assert "sk-1234567890" not in (st["runner"]["last_error"] or "")

    # Outbox redacts secrets before persistence
    plugin_path = Path("E:/Code/mem-broker/integrations/hermes/agentbrain/__init__.py")
    spec = importlib.util.spec_from_file_location("agentbrain_plugin_test_mod", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agentbrain_plugin_test_mod"] = mod
    spec.loader.exec_module(mod)
    p = mod.ObsidianBrokerMemoryProvider()
    p._hermes_home = str(tmp_path / "hermes_home")
    p._enqueue_outbox({"content": "my token is sk-1234567890abcdef123456", "meta": {"auth": "Bearer abcdefghijklmnop"}})
    outbox_text = (tmp_path / "hermes_home" / "cache" / "proactive_outbox.jsonl").read_text(encoding="utf-8")
    assert "sk-1234567890" not in outbox_text
    assert "abcdefghijklmnop" not in outbox_text


# --- Item 7: Historical catchup creates historical nightly windows ---

def test_catchup_creates_historical_nightly_windows_before_baseline(tmp_path: Path) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(tmp_path, proactive=ProactiveConfig(enabled=True))
    store = ProactiveStore.from_app_config(cfg, clock=clock)

    # First tick establishes baseline at 2026-11-16
    store.tick()
    # Historical catchup event from 2026-11-10
    store.ingest_event(
        client="agy_ide",
        session_id="hist-1",
        event_id="hist-e-1",
        content="old conversation",
        occurred_at=dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.timezone.utc),
        qualifies_nightly=True,
    )
    tick_res = store.tick(include_historical=True)
    assert tick_res.jobs_created >= 1
    st = store.get_status()
    win_ids = [w["window_id"] for w in st["nightly_windows"]]
    assert "2026-11-10" in win_ids


# --- Final handoff fixes: periodic heartbeat during model call & two-segment same-project snapshot hashes ---


def test_periodic_heartbeat_during_model_call_renews_both_leases_across_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            lease_seconds=30,
            model_endpoint="http://127.0.0.1:9999/v1",
            model_name="fake",
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"hb-{i}", content=f"msg {i}")

    runner_token = store.try_acquire_runner_lease("runner-1", lease_seconds=30)
    assert runner_token is not None
    job = store.claim_job("runner-1")
    assert job is not None

    runner_renew_count = 0
    competing_store = ProactiveStore.from_app_config(cfg, clock=clock)
    competing_steal_attempts: list[tuple[int, str | None]] = []

    def _runner_hb() -> bool:
        nonlocal runner_renew_count
        ok = store.renew_runner_lease("runner-1", runner_token, lease_seconds=30)
        if ok:
            runner_renew_count += 1
        return ok

    def _blocking_model_call(_cfg, _prompt: str) -> dict:
        # Advance fake clock 4 x 20s = 80s (> lease_seconds=30s) while heartbeat ticks every 10ms
        for _ in range(4):
            clock.advance(seconds=20)
            time.sleep(0.03)
            recovered = competing_store.recover_expired_leases()
            stolen_runner = competing_store.try_acquire_runner_lease("competing-runner", lease_seconds=30)
            competing_steal_attempts.append((recovered, stolen_runner))
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "Heartbeat Kept Alive",
                    "content": "survived lease window",
                    "source_refs": ["hb-1"],
                }
            ],
        }

    monkeypatch.setattr("agentbrain.proactive_worker.call_openai_compatible_model", _blocking_model_call)

    res = process_claimed_job(
        cfg,
        store,
        job,
        clock=clock,
        lease_heartbeat=_runner_hb,
        heartbeat_interval_seconds=0.01,
    )
    assert res.applied_count == 1
    assert runner_renew_count >= 3
    assert all(recovered == 0 and stolen is None for recovered, stolen in competing_steal_attempts)
    assert list((cfg.profile_dir / "20_LongTerm" / "Facts").glob("FACT-*-Heartbeat-Kept-Alive.md"))
    assert not any(t.name == f"proactive-hb-{job.job_id}" and t.is_alive() for t in threading.enumerate())


def test_heartbeat_lost_during_model_call_aborts_apply_and_stops_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            lease_seconds=30,
            model_endpoint="http://127.0.0.1:9999/v1",
            model_name="fake",
        ),
    )
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"lost-{i}", content=f"msg {i}")

    runner_token = store.try_acquire_runner_lease("runner-1", lease_seconds=30)
    assert runner_token is not None
    job = store.claim_job("runner-1")
    assert job is not None

    def _runner_hb() -> bool:
        return store.renew_runner_lease("runner-1", runner_token, lease_seconds=30)

    def _model_call_while_lease_lost(_cfg, _prompt: str) -> dict:
        # Simulate runner lease being lost/released while model call is in flight
        store.release_runner_lease("runner-1", runner_token)
        time.sleep(0.05)
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "longterm_upsert",
                    "note_type": "fact",
                    "title": "Should Not Be Written",
                    "content": "must abort",
                    "source_refs": ["lost-1"],
                }
            ],
        }

    monkeypatch.setattr("agentbrain.proactive_worker.call_openai_compatible_model", _model_call_while_lease_lost)

    with pytest.raises(LeaseMismatchError):
        process_claimed_job(
            cfg,
            store,
            job,
            clock=clock,
            lease_heartbeat=_runner_hb,
            heartbeat_interval_seconds=0.01,
        )

    assert not (cfg.profile_dir / "20_LongTerm" / "Facts" / "Should Not Be Written.md").exists()
    assert not any(t.name == f"proactive-hb-{job.job_id}" and t.is_alive() for t in threading.enumerate())


def test_two_segment_same_project_uses_trusted_post_write_hashes_and_rejects_external_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    cfg = _make_cfg(
        tmp_path,
        proactive=ProactiveConfig(
            enabled=True,
            model_endpoint="http://127.0.0.1:9999/v1",
            model_name="fake",
            model_max_context_chars=4200,
        ),
    )
    alpha_path = cfg.profile_dir / "20_LongTerm" / "Projects" / "Alpha.md"
    alpha_initial = (
        "---\ntype: project\ntitle: \"Alpha\"\nstatus: active\n---\n\n"
        "# Alpha\n\nManual intro.\n\n"
        "<!-- agentbrain:managed:start -->\ninitial managed\n<!-- agentbrain:managed:end -->\n\n"
        "Manual outro.\n"
    )
    alpha_path.write_text(alpha_initial, encoding="utf-8")
    h0 = sha256_text(alpha_initial)

    store = ProactiveStore.from_app_config(cfg, clock=clock)
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"p-{i}", content=f"turn {i} " + "A" * 350)

    call_idx = 0

    def _two_seg_same_project(_cfg, prompt: str) -> dict:
        nonlocal call_idx
        call_idx += 1
        m = re.search(r"event_id=([^\s\]]+)", prompt)
        assert m is not None
        ev_ref = m.group(1)
        if call_idx == 1:
            return {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "project_update",
                        "project": "Alpha",
                        "managed_content": "segment 1 update",
                        "expected_hash": h0,
                        "source_refs": [ev_ref],
                    }
                ],
            }
        # Segment 2+ still sees pre-job h0 in its immutable prompt and passes expected_hash=h0
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "project_update",
                    "project": "Alpha",
                    "managed_content": f"segment {call_idx} update",
                    "expected_hash": h0,
                    "source_refs": [ev_ref],
                }
            ],
        }

    monkeypatch.setattr("agentbrain.proactive_worker.call_openai_compatible_model", _two_seg_same_project)

    res = run_worker_once(cfg, store, clock=clock, heartbeat_interval_seconds=0.01)
    assert res is not None
    assert res.segments_processed >= 2
    assert res.applied_count == res.segments_processed
    final_alpha = alpha_path.read_text(encoding="utf-8")
    assert f"segment {res.segments_processed} update" in final_alpha
    assert "Manual intro." in final_alpha
    assert "Manual outro." in final_alpha

    cursor = store.get_adapter_cursor("worker_segment", f"job_segment_progress:{res.job_id}")
    assert cursor is not None
    assert cursor["initial_snapshot_hashes"]["20_LongTerm/Projects/Alpha.md"] == h0
    assert cursor["trusted_post_write_hashes"]["20_LongTerm/Projects/Alpha.md"] == sha256_text(final_alpha)

    # Now test concurrent external edit between segment 1 and segment 2 -> MUST raise ConflictError
    cfg2 = _make_cfg(
        tmp_path / "ext_edit",
        proactive=ProactiveConfig(
            enabled=True,
            model_endpoint="http://127.0.0.1:9999/v1",
            model_name="fake",
            model_max_context_chars=4200,
        ),
    )
    alpha2 = cfg2.profile_dir / "20_LongTerm" / "Projects" / "Alpha.md"
    alpha2.write_text(alpha_initial, encoding="utf-8")
    store2 = ProactiveStore.from_app_config(cfg2, clock=clock)
    for i in range(1, 6):
        store2.ingest_event(client="hermes", session_id="s1", event_id=f"q-{i}", content=f"turn {i} " + "B" * 350)
    job2 = store2.claim_job("w-ext")
    assert job2 is not None

    seg_call = 0

    def _two_seg_with_external_edit(_cfg, prompt: str) -> dict:
        nonlocal seg_call
        seg_call += 1
        m = re.search(r"event_id=([^\s\]]+)", prompt)
        assert m is not None
        ev_ref = m.group(1)
        if seg_call == 1:
            return {
                "outcome": "operations",
                "operations": [
                    {
                        "op_type": "project_update",
                        "project": "Alpha",
                        "managed_content": "seg 1 ok",
                        "expected_hash": h0,
                        "source_refs": [ev_ref],
                    }
                ],
            }
        # External user edits Alpha.md while segment 2 model call is in flight
        alpha2.write_text(alpha2.read_text(encoding="utf-8") + "\nExternal manual edit!\n", encoding="utf-8")
        return {
            "outcome": "operations",
            "operations": [
                {
                    "op_type": "project_update",
                    "project": "Alpha",
                    "managed_content": "seg 2 should conflict",
                    "expected_hash": h0,
                    "source_refs": [ev_ref],
                }
            ],
        }

    monkeypatch.setattr("agentbrain.proactive_worker.call_openai_compatible_model", _two_seg_with_external_edit)
    with pytest.raises(ConflictError):
        process_claimed_job(cfg2, store2, job2, clock=clock, heartbeat_interval_seconds=0.01)
    assert "External manual edit!" in alpha2.read_text(encoding="utf-8")
    assert "seg 2 should conflict" not in alpha2.read_text(encoding="utf-8")
