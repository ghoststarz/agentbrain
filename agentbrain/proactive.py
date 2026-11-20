"""主动记忆整理 Stage 1：耐久 SQLite 事件采集、去重、计数与任务调度核心。

职责边界（Stage 1）：
- 运行时数据库位于 broker.state_dir（不进入 vault），按 profile 隔离；
- 事务级事件去重（client, session_id, event_id）与每会话完成人类轮次耐久计数；
- 满 N 轮（默认 5）触发固定批次增量评审任务，严格保持同会话批次顺序；
- 基于 OS 本地壁钟 23:00（DST 安全）推进夜间窗口 [前日 23:00, 当日 23:00)，
  持久化激活基线，跳过空窗口（不创建任务/不调用模型），支持迟到数据重开补录；
- 原子认领任务（BEGIN IMMEDIATE）、租约过期回收、指数退避重试、no_change 确认、
  状态脱敏（不含聊天原文）与安全保留期清理（不删待处理证据）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agentbrain.acl import display_client_id
from agentbrain.config import AppConfig, ProactiveConfig
from agentbrain.proactive_clock import (
    Clock,
    SystemClock,
    compute_nightly_window,
    dt_to_us,
    ensure_utc,
    latest_closed_window_date,
    us_to_iso,
)
from agentbrain.proactive_redaction import redact_secrets

SCHEMA_VERSION = 3

EVENT_KIND_HUMAN_TURN = "human_turn"
EVENT_KIND_REFERENCE_ONLY = "reference_only"
EXCLUDED_EVENT_KINDS = frozenset({"system", "heartbeat", "review_generated"})
VALID_EVENT_KINDS = frozenset({EVENT_KIND_HUMAN_TURN, EVENT_KIND_REFERENCE_ONLY}) | EXCLUDED_EVENT_KINDS

SYSTEM_NOTICE_PREFIXES = (
    "[IMPORTANT:",
    "[INTERNAL NOTIFICATION",
    "[PREVIOUS_REVIEW_RESULT]",
    "[DIARY]",
)
_MULTI_BLANK_LINES_RE = re.compile(r"(?:\n[ \t]*){2,}")


def extract_user_segment(text: str) -> str:
    """从 'User: ...\\nAssistant: ...' 格式中提取首段 User 正文；若无前缀则返回原文。"""
    s = (text or "").lstrip()
    if s.startswith("User:"):
        after_user = s[len("User:") :]
        idx = after_user.find("\nAssistant:")
        if idx >= 0:
            return after_user[:idx]
        return after_user
    return text or ""


def normalize_user_text(text: str) -> str:
    """归一化用户文本：统一换行、去首尾空白、合并连续空行。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    return _MULTI_BLANK_LINES_RE.sub("\n\n", normalized)


def classify_user_turn(text: str) -> str:
    """将用户消息分类为 'system_notice'（不可计数）或 'user'（可计数）。"""
    user_part = extract_user_segment(text)
    norm = normalize_user_text(user_part)
    if norm.startswith(SYSTEM_NOTICE_PREFIXES) or "heartbeat #" in norm.lower():
        return "system_notice"
    return "user"

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_RETRY_WAIT = "retry_wait"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_DEAD = "dead"
ACTIVE_JOB_STATUSES = (JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JOB_STATUS_RETRY_WAIT)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    countable INTEGER NOT NULL,
    qualifies_nightly INTEGER NOT NULL,
    turn_seq INTEGER,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_us INTEGER NOT NULL,
    ingested_at_utc TEXT NOT NULL,
    ingested_at_us INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_truncated INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT,
    route TEXT,
    paired_route TEXT,
    UNIQUE(client, session_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session_turn
    ON events(client, session_id, turn_seq);
CREATE INDEX IF NOT EXISTS idx_events_occurred
    ON events(occurred_at_us, id);

CREATE TABLE IF NOT EXISTS session_counters (
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    completed_human_turns INTEGER NOT NULL DEFAULT 0,
    last_scheduled_turn INTEGER NOT NULL DEFAULT 0,
    last_reviewed_turn INTEGER NOT NULL DEFAULT 0,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (client, session_id)
);

CREATE TABLE IF NOT EXISTS nightly_windows (
    window_id TEXT PRIMARY KEY,
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    offset_start_seconds INTEGER NOT NULL,
    offset_end_seconds INTEGER NOT NULL,
    tz_label TEXT NOT NULL,
    status TEXT NOT NULL,
    last_revision INTEGER NOT NULL DEFAULT 0,
    completed_revision INTEGER NOT NULL DEFAULT 0,
    watermark_event_id INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    client TEXT,
    session_id TEXT,
    batch_start INTEGER,
    batch_end INTEGER,
    window_id TEXT,
    revision INTEGER,
    since_event_id INTEGER,
    until_event_id INTEGER,
    event_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    worker_id TEXT,
    lease_token TEXT,
    claimed_at_us INTEGER,
    lease_expires_at_us INTEGER,
    next_retry_at_us INTEGER,
    last_error TEXT,
    outcome TEXT,
    result_summary_json TEXT,
    created_at_utc TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    finished_at_us INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_retry
    ON jobs(status, next_retry_at_us);
CREATE INDEX IF NOT EXISTS idx_jobs_inc_order
    ON jobs(job_type, client, session_id, batch_start);
CREATE INDEX IF NOT EXISTS idx_jobs_nightly_order
    ON jobs(job_type, window_id, revision);

CREATE TABLE IF NOT EXISTS event_chunks (
    event_row_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (event_row_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS scheduler_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    acquired_at_us INTEGER NOT NULL,
    expires_at_us INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS adapter_cursors (
    adapter_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY (adapter_name, source_key)
);

CREATE TABLE IF NOT EXISTS proactive_applied_ops (
    op_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    job_idempotency_key TEXT NOT NULL,
    op_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_date TEXT,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    applied_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proactive_ops_dedupe
    ON proactive_applied_ops(op_type, dedupe_key);

CREATE TABLE IF NOT EXISTS review_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    applied_count INTEGER NOT NULL DEFAULT 0,
    categories_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT,
    created_at_utc TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    delivered_at_utc TEXT,
    delivered_to_client TEXT,
    delivered_to_session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_receipts_pending
    ON review_receipts(delivered_at_utc, client, session_id, id);

CREATE TABLE IF NOT EXISTS archive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_path TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    mtime_us INTEGER NOT NULL,
    last_access_us INTEGER NOT NULL,
    archived_at_utc TEXT NOT NULL,
    archive_version INTEGER NOT NULL DEFAULT 1
);
"""


@dataclass(frozen=True)
class IngestResult:
    """事件写入结果。"""

    accepted: bool
    deduped: bool
    excluded: bool
    event_row_id: int | None
    turn_seq: int | None
    completed_human_turns: int
    created_job_id: int | None


@dataclass(frozen=True)
class StoredEvent:
    """存储在调度库中的单条事件证据（供 Stage 2 读取）。"""

    row_id: int
    client: str
    session_id: str
    event_id: str
    event_kind: str
    countable: bool
    qualifies_nightly: bool
    turn_seq: int | None
    occurred_at_utc: str
    occurred_at_us: int
    content: str
    content_truncated: bool
    meta: dict[str, Any]


@dataclass(frozen=True)
class ClaimedJob:
    """被 worker 原子认领的任务及其租约令牌。"""

    job_id: int
    idempotency_key: str
    job_type: str
    attempts: int
    max_attempts: int
    lease_token: str
    lease_expires_at_utc: str
    client: str | None
    session_id: str | None
    batch_start: int | None
    batch_end: int | None
    window_id: str | None
    revision: int | None
    since_event_id: int | None
    until_event_id: int | None
    event_count: int


@dataclass(frozen=True)
class TickResult:
    """一次夜间调度检查的结果摘要。"""

    windows_checked: int
    windows_skipped_empty: int
    jobs_created: int
    windows_reopened: int
    windows_requeued_dead: int = 0


class LeaseMismatchError(RuntimeError):
    """认领令牌失效或任务当前不处于可操作状态。"""


class PayloadTooLargeError(ValueError):
    """事件正文超出硬上限（拒绝静默截断）。"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _owner_process_alive(owner_id: str) -> bool:
    """判断租约 owner_id 里的 pid 是否仍存活（owner 形如 pid-1234-abcd1234）。

    只看过期时间不够：进程被重启后旧租约最长还会挡住新实例整整一个 lease_seconds，
    这期间后台复盘完全停摆。pid 已不存在时应立即允许接管。
    """
    raw = str(owner_id or "").strip()
    if not raw.startswith("pid-"):
        return True
    try:
        pid = int(raw[4:].split("-", 1)[0])
    except (ValueError, IndexError):
        return True
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return int(exit_code.value) == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _chunk_text(text: str, chunk_size: int, max_chunks: int) -> list[str]:
    """将正文无损切分为有界块；超出硬上限时显式拒绝，绝不静默截断。"""
    max_total = chunk_size * max_chunks
    if len(text) > max_total:
        raise PayloadTooLargeError(
            f"event content length {len(text)} exceeds max bounded capacity {max_total} "
            f"({max_chunks} chunks * {chunk_size} chars)"
        )
    if len(text) <= chunk_size:
        return [text]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _bound_meta(meta: dict[str, Any] | None, max_chars: int = 4096) -> str:
    if not meta:
        return "{}"
    encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return encoded
    return json.dumps({"_truncated": True, "_orig_len": len(encoded)}, separators=(",", ":"))


class ProactiveStore:
    """耐久 SQLite 主动评审事件与任务调度核心。"""

    def __init__(
        self,
        db_path: Path,
        *,
        config: ProactiveConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.config = config or ProactiveConfig()
        self.clock: Clock = clock or SystemClock()
        self.init_db()

    @classmethod
    def from_app_config(
        cls,
        app_config: AppConfig,
        *,
        clock: Clock | None = None,
    ) -> ProactiveStore:
        """从 AppConfig 构造 profile 隔离的调度库（位于 broker.state_dir）。"""
        app_config.broker.ensure_runtime_dirs()
        return cls(
            app_config.proactive_db_path,
            config=app_config.memory.proactive,
            clock=clock,
        )

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        """幂等初始化表结构、迁移版本与激活基线。"""
        with self._read() as conn:
            conn.executescript(SCHEMA_SQL)
        with self._tx() as conn:
            self._ensure_migrations(conn)
            self._ensure_baseline(conn)

    def _ensure_migrations(self, conn: sqlite3.Connection) -> None:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        for col_name in ("content_hash", "route", "paired_route"):
            if col_name not in cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col_name} TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_cross_route "
            "ON events(client, session_id, content_hash, occurred_at_us)"
        )
        row = conn.execute(
            "SELECT value FROM scheduler_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO scheduler_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) < SCHEMA_VERSION:
            conn.execute(
                "UPDATE scheduler_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )

    def _ensure_baseline(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM scheduler_meta WHERE key = 'activation_baseline_utc'"
        ).fetchone()
        if row is not None:
            return
        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        last_closed = latest_closed_window_date(
            now_utc,
            self.clock,
            hour=self.config.nightly_hour,
            minute=self.config.nightly_minute,
        )
        entries = (
            ("activation_baseline_utc", now_utc.isoformat()),
            ("activation_baseline_us", str(now_us)),
            ("baseline_last_closed_date", last_closed.isoformat()),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO scheduler_meta (key, value) VALUES (?, ?)",
            entries,
        )

    def ingest_event(
        self,
        *,
        client: str,
        session_id: str,
        event_id: str,
        event_kind: str = EVENT_KIND_HUMAN_TURN,
        content: str = "",
        occurred_at: dt.datetime | None = None,
        meta: dict[str, Any] | None = None,
        qualifies_nightly: bool = True,
        route: str | None = None,
    ) -> IngestResult:
        """事务级写入事件并去重；人类轮次累计满批即创建增量任务。"""
        if event_kind not in VALID_EVENT_KINDS:
            raise ValueError(f"不支持的 event_kind: {event_kind!r}")
        if not client.strip() or not session_id.strip() or not event_id.strip():
            raise ValueError("client, session_id, event_id 均不能为空")

        if event_kind in EXCLUDED_EVENT_KINDS:
            with self._read() as conn:
                cnt = self._get_completed_turns(conn, client, session_id)
            return IngestResult(
                accepted=False,
                deduped=False,
                excluded=True,
                event_row_id=None,
                turn_seq=None,
                completed_human_turns=cnt,
                created_job_id=None,
            )

        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        occ_utc = ensure_utc(occurred_at) if occurred_at is not None else now_utc
        occ_us = dt_to_us(occ_utc)

        sanitized_content = redact_secrets(content)
        chunks = _chunk_text(
            sanitized_content,
            self.config.max_payload_chars,
            self.config.max_payload_chunks,
        )
        primary_content = chunks[0]
        content_hash = hashlib.sha256(sanitized_content.strip().encode("utf-8")).hexdigest()
        eff_route = (route or (str(meta.get("route") or "") if isinstance(meta, dict) else "") or "").strip() or None
        meta_json = _bound_meta(meta)
        is_countable = 1 if event_kind == EVENT_KIND_HUMAN_TURN else 0
        nightly_flag = 1 if qualifies_nightly else 0

        with self._tx() as conn:
            existing = conn.execute(
                """
                SELECT id, turn_seq FROM events
                WHERE client = ? AND session_id = ? AND event_id = ?
                """,
                (client, session_id, event_id),
            ).fetchone()
            if existing is not None:
                cnt = self._get_completed_turns(conn, client, session_id)
                return IngestResult(
                    accepted=True,
                    deduped=True,
                    excluded=False,
                    event_row_id=int(existing["id"]),
                    turn_seq=int(existing["turn_seq"]) if existing["turn_seq"] is not None else None,
                    completed_human_turns=cnt,
                    created_job_id=None,
                )

            if eff_route in ("provider", "state_db"):
                other_route = "state_db" if eff_route == "provider" else "provider"
                win_lo = occ_us - 120_000_000
                win_hi = occ_us + 120_000_000
                paired = conn.execute(
                    """
                    SELECT id, turn_seq FROM events
                    WHERE client = ? AND session_id = ? AND content_hash = ?
                      AND route = ? AND (paired_route IS NULL OR paired_route = '')
                      AND occurred_at_us BETWEEN ? AND ?
                    ORDER BY ABS(occurred_at_us - ?) ASC, id ASC
                    LIMIT 1
                    """,
                    (client, session_id, content_hash, other_route, win_lo, win_hi, occ_us),
                ).fetchone()
                if paired is not None:
                    conn.execute(
                        "UPDATE events SET paired_route = ? WHERE id = ?",
                        (eff_route, int(paired["id"])),
                    )
                    cnt = self._get_completed_turns(conn, client, session_id)
                    return IngestResult(
                        accepted=True,
                        deduped=True,
                        excluded=False,
                        event_row_id=int(paired["id"]),
                        turn_seq=int(paired["turn_seq"]) if paired["turn_seq"] is not None else None,
                        completed_human_turns=cnt,
                        created_job_id=None,
                    )

            conn.execute(
                """
                INSERT OR IGNORE INTO session_counters (
                    client, session_id, completed_human_turns,
                    last_scheduled_turn, last_reviewed_turn, updated_at_utc
                ) VALUES (?, ?, 0, 0, 0, ?)
                """,
                (client, session_id, now_utc.isoformat()),
            )
            counter = conn.execute(
                """
                SELECT completed_human_turns, last_scheduled_turn, last_reviewed_turn
                FROM session_counters
                WHERE client = ? AND session_id = ?
                """,
                (client, session_id),
            ).fetchone()
            assert counter is not None

            completed_turns = int(counter["completed_human_turns"])
            last_scheduled = int(counter["last_scheduled_turn"])
            last_reviewed = int(counter["last_reviewed_turn"])
            turn_seq: int | None = None
            created_job_id: int | None = None

            if is_countable:
                completed_turns += 1
                turn_seq = completed_turns
                conn.execute(
                    """
                    UPDATE session_counters
                    SET completed_human_turns = ?, updated_at_utc = ?
                    WHERE client = ? AND session_id = ?
                    """,
                    (completed_turns, now_utc.isoformat(), client, session_id),
                )

            cur = conn.execute(
                """
                INSERT INTO events (
                    client, session_id, event_id, event_kind,
                    countable, qualifies_nightly, turn_seq,
                    occurred_at_utc, occurred_at_us,
                    ingested_at_utc, ingested_at_us,
                    content, content_truncated, meta_json,
                    content_hash, route
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client,
                    session_id,
                    event_id,
                    event_kind,
                    is_countable,
                    nightly_flag,
                    turn_seq,
                    occ_utc.isoformat(),
                    occ_us,
                    now_utc.isoformat(),
                    now_us,
                    primary_content,
                    0,
                    meta_json,
                    content_hash,
                    eff_route,
                ),
            )
            event_row_id = int(cur.lastrowid)
            if len(chunks) > 1:
                conn.executemany(
                    "INSERT INTO event_chunks (event_row_id, chunk_index, content) VALUES (?, ?, ?)",
                    [(event_row_id, idx, chunk) for idx, chunk in enumerate(chunks)],
                )

            batch_size = self.config.incremental_batch_size
            if (
                is_countable
                and completed_turns > last_reviewed
                and (completed_turns - last_scheduled) >= batch_size
                and (completed_turns - last_reviewed) >= batch_size
            ):
                batch_start = last_scheduled + 1
                batch_end = last_scheduled + batch_size
                idem_key = f"inc:{client}:{session_id}:{batch_start}:{batch_end}"
                job_cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs (
                        idempotency_key, job_type, status,
                        client, session_id, batch_start, batch_end,
                        event_count, attempts, max_attempts,
                        created_at_utc, created_at_us
                    ) VALUES (?, 'incremental', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        idem_key,
                        JOB_STATUS_PENDING,
                        client,
                        session_id,
                        batch_start,
                        batch_end,
                        batch_size,
                        self.config.max_attempts,
                        now_utc.isoformat(),
                        now_us,
                    ),
                )
                if job_cur.rowcount > 0:
                    created_job_id = int(job_cur.lastrowid)
                conn.execute(
                    """
                    UPDATE session_counters
                    SET last_scheduled_turn = ?, updated_at_utc = ?
                    WHERE client = ? AND session_id = ?
                    """,
                    (batch_end, now_utc.isoformat(), client, session_id),
                )

            return IngestResult(
                accepted=True,
                deduped=False,
                excluded=False,
                event_row_id=event_row_id,
                turn_seq=turn_seq,
                completed_human_turns=completed_turns,
                created_job_id=created_job_id,
            )

    @staticmethod
    def _get_completed_turns(conn: sqlite3.Connection, client: str, session_id: str) -> int:
        row = conn.execute(
            """
            SELECT completed_human_turns FROM session_counters
            WHERE client = ? AND session_id = ?
            """,
            (client, session_id),
        ).fetchone()
        return int(row["completed_human_turns"]) if row is not None else 0

    def tick(self, *, include_historical: bool = False) -> TickResult:
        """在启动或定时 tick 时按时间顺序追平已关闭夜间窗口并检测迟到数据。"""
        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        hour = self.config.nightly_hour
        minute = self.config.nightly_minute

        windows_checked = 0
        windows_skipped_empty = 0
        jobs_created = 0
        windows_reopened = 0
        windows_requeued_dead = 0

        with self._tx() as conn:
            self._ensure_baseline(conn)
            base_row = conn.execute(
                "SELECT value FROM scheduler_meta WHERE key = 'baseline_last_closed_date'"
            ).fetchone()
            assert base_row is not None
            baseline_date = dt.date.fromisoformat(str(base_row["value"]))
            latest_closed = latest_closed_window_date(now_utc, self.clock, hour=hour, minute=minute)

            # 1. 按时间顺序追平尚未记录的已关闭窗口（baseline 之后；显式 catchup 时包含历史事件最早窗口）
            cur_date = baseline_date + dt.timedelta(days=1)
            if include_historical:
                min_row = conn.execute(
                    "SELECT MIN(occurred_at_us) AS min_us FROM events WHERE qualifies_nightly = 1"
                ).fetchone()
                if min_row is not None and min_row["min_us"] is not None:
                    earliest_utc = dt.datetime.fromtimestamp(
                        int(min_row["min_us"]) / 1_000_000,
                        tz=dt.timezone.utc,
                    )
                    earliest_local = self.clock.to_local(earliest_utc)
                    earliest_date = earliest_local.date()
                    if earliest_date < cur_date:
                        cur_date = earliest_date
            while cur_date <= latest_closed:
                win_id = cur_date.isoformat()
                existing = conn.execute(
                    "SELECT window_id FROM nightly_windows WHERE window_id = ?",
                    (win_id,),
                ).fetchone()
                if existing is None:
                    windows_checked += 1
                    spec = compute_nightly_window(cur_date, self.clock, hour=hour, minute=minute)
                    stats = conn.execute(
                        """
                        SELECT COUNT(*) AS cnt, COALESCE(MAX(id), 0) AS max_id
                        FROM events
                        WHERE qualifies_nightly = 1
                          AND occurred_at_us >= ?
                          AND occurred_at_us < ?
                        """,
                        (spec.start_us, spec.end_us),
                    ).fetchone()
                    cnt = int(stats["cnt"])
                    max_id = int(stats["max_id"])

                    if cnt == 0:
                        windows_skipped_empty += 1
                        conn.execute(
                            """
                            INSERT INTO nightly_windows (
                                window_id, window_start_utc, window_end_utc,
                                window_start_us, window_end_us,
                                offset_start_seconds, offset_end_seconds, tz_label,
                                status, last_revision, completed_revision, watermark_event_id,
                                created_at_utc, updated_at_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'skipped_empty', 0, 0, 0, ?, ?)
                            """,
                            (
                                spec.window_id,
                                spec.start_utc.isoformat(),
                                spec.end_utc.isoformat(),
                                spec.start_us,
                                spec.end_us,
                                spec.offset_start_seconds,
                                spec.offset_end_seconds,
                                spec.tz_label,
                                now_utc.isoformat(),
                                now_utc.isoformat(),
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO nightly_windows (
                                window_id, window_start_utc, window_end_utc,
                                window_start_us, window_end_us,
                                offset_start_seconds, offset_end_seconds, tz_label,
                                status, last_revision, completed_revision, watermark_event_id,
                                created_at_utc, updated_at_utc
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 1, 0, ?, ?, ?)
                            """,
                            (
                                spec.window_id,
                                spec.start_utc.isoformat(),
                                spec.end_utc.isoformat(),
                                spec.start_us,
                                spec.end_us,
                                spec.offset_start_seconds,
                                spec.offset_end_seconds,
                                spec.tz_label,
                                max_id,
                                now_utc.isoformat(),
                                now_utc.isoformat(),
                            ),
                        )
                        if self._enqueue_nightly_job(
                            conn,
                            window_id=spec.window_id,
                            revision=1,
                            since_event_id=0,
                            until_event_id=max_id,
                            event_count=cnt,
                            now_utc=now_utc,
                            now_us=now_us,
                        ):
                            jobs_created += 1
                cur_date += dt.timedelta(days=1)

            # 2. 检查所有已记录窗口是否有迟到导入的新证据（id > watermark_event_id）
            recorded = conn.execute(
                """
                SELECT window_id, window_start_us, window_end_us,
                       status, last_revision, watermark_event_id
                FROM nightly_windows
                ORDER BY window_end_us ASC, window_id ASC
                """
            ).fetchall()
            for win in recorded:
                watermark = int(win["watermark_event_id"])
                late = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt, COALESCE(MAX(id), 0) AS max_id
                    FROM events
                    WHERE qualifies_nightly = 1
                      AND occurred_at_us >= ?
                      AND occurred_at_us < ?
                      AND id > ?
                    """,
                    (int(win["window_start_us"]), int(win["window_end_us"]), watermark),
                ).fetchone()
                late_cnt = int(late["cnt"])
                if late_cnt == 0:
                    continue

                new_max_id = int(late["max_id"])
                new_rev = int(win["last_revision"]) + 1
                win_id = str(win["window_id"])
                new_status = "reopened" if win["status"] in ("skipped_empty", "completed") else str(win["status"])

                conn.execute(
                    """
                    UPDATE nightly_windows
                    SET status = ?, last_revision = ?, watermark_event_id = ?, updated_at_utc = ?
                    WHERE window_id = ?
                    """,
                    (new_status, new_rev, new_max_id, now_utc.isoformat(), win_id),
                )
                windows_reopened += 1
                if self._enqueue_nightly_job(
                    conn,
                    window_id=win_id,
                    revision=new_rev,
                    since_event_id=watermark,
                    until_event_id=new_max_id,
                    event_count=late_cnt,
                    now_utc=now_utc,
                    now_us=now_us,
                ):
                    jobs_created += 1

            # 3. 兜底补齐：窗口存在、当前 revision 还没完成
            #    3a) 若同 window_id + 同 last_revision 的 nightly 任务处于 dead，则原位复位为 pending
            dead_nightly = conn.execute(
                """
                SELECT j.id, j.window_id, j.revision, j.last_error
                FROM nightly_windows nw
                JOIN jobs j
                  ON j.job_type = 'nightly'
                 AND j.window_id = nw.window_id
                 AND j.revision = nw.last_revision
                 AND j.status = 'dead'
                WHERE nw.last_revision > nw.completed_revision
                  AND nw.status IN ('pending', 'scheduled', 'reopened')
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j2
                      WHERE j2.job_type = 'nightly'
                        AND j2.window_id = nw.window_id
                        AND j2.revision = nw.last_revision
                        AND j2.status != 'dead'
                  )
                ORDER BY nw.window_end_us ASC, nw.window_id ASC, j.id ASC
                """
            ).fetchall()
            for drow in dead_nightly:
                dead_job_id = int(drow["id"])
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        attempts = 0,
                        lease_token = NULL,
                        worker_id = NULL,
                        lease_expires_at_us = NULL,
                        next_retry_at_us = NULL,
                        finished_at_us = NULL
                    WHERE id = ? AND status = 'dead'
                    """,
                    (JOB_STATUS_PENDING, dead_job_id),
                )
                if cur.rowcount > 0:
                    conn.execute(
                        "DELETE FROM adapter_cursors WHERE adapter_name = 'worker_segment' AND source_key = ?",
                        (f"job_segment_progress:{dead_job_id}",),
                    )
                    err_summary = redact_secrets(str(drow["last_error"] or "dead_nightly_requeued"))[:160]
                    conn.execute(
                        """
                        INSERT INTO review_receipts (
                            job_id, client, session_id, job_type, outcome,
                            applied_count, categories_json, error_summary,
                            created_at_utc, created_at_us
                        ) VALUES (?, 'nightly', '', 'nightly', 'retrying_after_dead', 0, '{}', ?, ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            client = excluded.client,
                            session_id = excluded.session_id,
                            job_type = excluded.job_type,
                            outcome = excluded.outcome,
                            applied_count = excluded.applied_count,
                            categories_json = excluded.categories_json,
                            error_summary = excluded.error_summary,
                            created_at_utc = excluded.created_at_utc,
                            created_at_us = excluded.created_at_us,
                            delivered_at_utc = NULL,
                            delivered_to_client = NULL,
                            delivered_to_session_id = NULL
                        """,
                        (dead_job_id, err_summary, now_utc.isoformat(), now_us),
                    )
                    windows_requeued_dead += 1

            #    3b) 若根本没有对应任务（外部手改状态、任务被清理、或上次 enqueue 失败），补插任务。
            outstanding = conn.execute(
                """
                SELECT nw.window_id, nw.last_revision, nw.completed_revision,
                       nw.watermark_event_id, nw.window_start_us, nw.window_end_us
                FROM nightly_windows nw
                WHERE nw.last_revision > nw.completed_revision
                  AND nw.status IN ('pending', 'scheduled', 'reopened')
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.job_type = 'nightly'
                        AND j.window_id = nw.window_id
                        AND j.revision = nw.last_revision
                        AND j.status != 'dead'
                  )
                ORDER BY nw.window_end_us ASC, nw.window_id ASC
                """
            ).fetchall()
            for win in outstanding:
                win_id = str(win["window_id"])
                rev = int(win["last_revision"])
                watermark = int(win["watermark_event_id"] or 0)
                stats = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM events
                    WHERE qualifies_nightly = 1
                      AND occurred_at_us >= ?
                      AND occurred_at_us < ?
                      AND id <= ?
                    """,
                    (int(win["window_start_us"]), int(win["window_end_us"]), watermark),
                ).fetchone()
                cnt = int(stats["cnt"]) if stats is not None else 0
                if cnt == 0:
                    continue
                if self._enqueue_nightly_job(
                    conn,
                    window_id=win_id,
                    revision=rev,
                    since_event_id=0,
                    until_event_id=watermark,
                    event_count=cnt,
                    now_utc=now_utc,
                    now_us=now_us,
                ):
                    jobs_created += 1

        return TickResult(
            windows_checked=windows_checked,
            windows_skipped_empty=windows_skipped_empty,
            jobs_created=jobs_created,
            windows_reopened=windows_reopened,
            windows_requeued_dead=windows_requeued_dead,
        )

    def _enqueue_nightly_job(
        self,
        conn: sqlite3.Connection,
        *,
        window_id: str,
        revision: int,
        since_event_id: int,
        until_event_id: int,
        event_count: int,
        now_utc: dt.datetime,
        now_us: int,
    ) -> bool:
        idem_key = f"nightly:{window_id}:rev:{revision}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO jobs (
                idempotency_key, job_type, status,
                window_id, revision, since_event_id, until_event_id,
                event_count, attempts, max_attempts,
                created_at_utc, created_at_us
            ) VALUES (?, 'nightly', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                idem_key,
                JOB_STATUS_PENDING,
                window_id,
                revision,
                since_event_id,
                until_event_id,
                event_count,
                self.config.max_attempts,
                now_utc.isoformat(),
                now_us,
            ),
        )
        return cur.rowcount > 0

    def recover_expired_leases(self) -> int:
        """回收已过期的 running 租约，使其他 claimer 可重新认领或标记 dead。"""
        now_us = dt_to_us(self.clock.now_utc())
        with self._tx() as conn:
            return self._recover_expired_leases_tx(conn, now_us)

    @staticmethod
    def _recover_expired_leases_tx(conn: sqlite3.Connection, now_us: int) -> int:
        expired = conn.execute(
            """
            SELECT id, attempts, max_attempts FROM jobs
            WHERE status = 'running' AND lease_expires_at_us IS NOT NULL AND lease_expires_at_us <= ?
            """,
            (now_us,),
        ).fetchall()
        recovered = 0
        for row in expired:
            if int(row["attempts"]) >= int(row["max_attempts"]):
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'dead', lease_token = NULL, worker_id = NULL,
                        last_error = COALESCE(last_error, 'lease_expired'),
                        finished_at_us = ?
                    WHERE id = ?
                    """,
                    (now_us, int(row["id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'pending', lease_token = NULL, worker_id = NULL,
                        lease_expires_at_us = NULL, next_retry_at_us = NULL,
                        last_error = 'lease_expired'
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
            recovered += 1
        return recovered

    _CLAIM_CANDIDATE_SQL = """
        SELECT j.* FROM jobs j
        WHERE j.status IN ('pending', 'retry_wait')
          AND (j.next_retry_at_us IS NULL OR j.next_retry_at_us <= ?)
          AND (
              j.job_type != 'incremental'
              OR NOT EXISTS (
                  SELECT 1 FROM jobs prev
                  WHERE prev.job_type = 'incremental'
                    AND prev.client = j.client
                    AND prev.session_id = j.session_id
                    AND prev.batch_start < j.batch_start
                    AND prev.status != 'succeeded'
              )
          )
          AND (
              j.job_type != 'nightly'
              OR NOT EXISTS (
                  SELECT 1 FROM jobs prev
                  WHERE prev.job_type = 'nightly'
                    AND prev.status != 'succeeded'
                    AND (
                        prev.window_id < j.window_id
                        OR (prev.window_id = j.window_id AND prev.revision < j.revision)
                    )
              )
          )
        ORDER BY
            j.created_at_us ASC,
            j.window_id ASC,
            j.revision ASC,
            j.id ASC
        LIMIT 1
    """

    def peek_next_claimable_job(self) -> ClaimedJob | None:
        """只读预览下一个可执行任务（用于 dry-run，不修改队列、尝试次数或租约）。"""
        now_us = dt_to_us(self.clock.now_utc())
        with self._read() as conn:
            candidate = conn.execute(self._CLAIM_CANDIDATE_SQL, (now_us,)).fetchone()
            if candidate is None:
                return None
            return ClaimedJob(
                job_id=int(candidate["id"]),
                idempotency_key=str(candidate["idempotency_key"]),
                job_type=str(candidate["job_type"]),
                attempts=int(candidate["attempts"]),
                max_attempts=int(candidate["max_attempts"]),
                lease_token="dry-run",
                lease_expires_at_utc="",
                client=candidate["client"],
                session_id=candidate["session_id"],
                batch_start=int(candidate["batch_start"]) if candidate["batch_start"] is not None else None,
                batch_end=int(candidate["batch_end"]) if candidate["batch_end"] is not None else None,
                window_id=candidate["window_id"],
                revision=int(candidate["revision"]) if candidate["revision"] is not None else None,
                since_event_id=int(candidate["since_event_id"]) if candidate["since_event_id"] is not None else None,
                until_event_id=int(candidate["until_event_id"]) if candidate["until_event_id"] is not None else None,
                event_count=int(candidate["event_count"]),
            )

    def claim_job(
        self,
        worker_id: str,
        *,
        lease_seconds: int | None = None,
    ) -> ClaimedJob | None:
        """原子认领下一个可执行任务；保证同会话增量批次与同窗口修订版不乱序并发。"""
        lease_sec = lease_seconds if lease_seconds is not None else self.config.lease_seconds
        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        expires_us = now_us + lease_sec * 1_000_000

        with self._tx() as conn:
            self._recover_expired_leases_tx(conn, now_us)
            candidate = conn.execute(self._CLAIM_CANDIDATE_SQL, (now_us,)).fetchone()
            if candidate is None:
                return None

            lease_token = uuid.uuid4().hex
            new_attempts = int(candidate["attempts"]) + 1
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = ?,
                    worker_id = ?,
                    lease_token = ?,
                    claimed_at_us = ?,
                    lease_expires_at_us = ?,
                    next_retry_at_us = NULL
                WHERE id = ? AND status IN ('pending', 'retry_wait')
                """,
                (
                    new_attempts,
                    worker_id,
                    lease_token,
                    now_us,
                    expires_us,
                    int(candidate["id"]),
                ),
            )

            return ClaimedJob(
                job_id=int(candidate["id"]),
                idempotency_key=str(candidate["idempotency_key"]),
                job_type=str(candidate["job_type"]),
                attempts=new_attempts,
                max_attempts=int(candidate["max_attempts"]),
                lease_token=lease_token,
                lease_expires_at_utc=us_to_iso(expires_us),
                client=candidate["client"],
                session_id=candidate["session_id"],
                batch_start=int(candidate["batch_start"]) if candidate["batch_start"] is not None else None,
                batch_end=int(candidate["batch_end"]) if candidate["batch_end"] is not None else None,
                window_id=candidate["window_id"],
                revision=int(candidate["revision"]) if candidate["revision"] is not None else None,
                since_event_id=int(candidate["since_event_id"]) if candidate["since_event_id"] is not None else None,
                until_event_id=int(candidate["until_event_id"]) if candidate["until_event_id"] is not None else None,
                event_count=int(candidate["event_count"]),
            )

    def has_valid_lease(self, job_id: int, lease_token: str) -> bool:
        """检查当前任务租约是否仍然有效（未过期且未被回收）。"""
        now_us = dt_to_us(self.clock.now_utc())
        with self._read() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE id = ? AND status = 'running' AND lease_token = ?
                  AND (lease_expires_at_us IS NULL OR lease_expires_at_us > ?)
                LIMIT 1
                """,
                (job_id, lease_token, now_us),
            ).fetchone()
            return row is not None

    def renew_lease(
        self,
        job_id: int,
        lease_token: str,
        *,
        lease_seconds: int | None = None,
    ) -> str:
        """延长运行中任务的租约，返回新的 lease_expires_at_utc。"""
        lease_sec = lease_seconds if lease_seconds is not None else self.config.lease_seconds
        now_us = dt_to_us(self.clock.now_utc())
        expires_us = now_us + lease_sec * 1_000_000
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET lease_expires_at_us = ?
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (expires_us, job_id, lease_token),
            )
            if cur.rowcount == 0:
                raise LeaseMismatchError(f"任务 {job_id} 租约无效或已不在 running 状态")
        return us_to_iso(expires_us)

    def ack_job_success(
        self,
        job_id: int,
        lease_token: str,
        *,
        outcome: str = "no_change",
        summary: dict[str, Any] | None = None,
    ) -> None:
        """确认任务成功（支持 applied 或 no_change），推进已评审进度与窗口完成度。"""
        if outcome not in ("applied", "no_change"):
            raise ValueError(f"outcome 必须为 'applied' 或 'no_change'，收到: {outcome!r}")
        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        summary_json = _bound_meta(summary) if summary is not None else None

        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT job_type, client, session_id, batch_end, window_id, revision
                FROM jobs
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (job_id, lease_token),
            ).fetchone()
            if row is None:
                raise LeaseMismatchError(f"任务 {job_id} 租约无效或已不在 running 状态")

            conn.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    outcome = ?,
                    result_summary_json = ?,
                    last_error = NULL,
                    lease_token = NULL,
                    lease_expires_at_us = NULL,
                    finished_at_us = ?
                WHERE id = ?
                """,
                (outcome, summary_json, now_us, job_id),
            )

            if row["job_type"] == "incremental":
                conn.execute(
                    """
                    UPDATE session_counters
                    SET last_reviewed_turn = MAX(last_reviewed_turn, ?),
                        updated_at_utc = ?
                    WHERE client = ? AND session_id = ?
                    """,
                    (
                        int(row["batch_end"]),
                        now_utc.isoformat(),
                        str(row["client"]),
                        str(row["session_id"]),
                    ),
                )
            elif row["job_type"] == "nightly":
                win_id = str(row["window_id"])
                rev = int(row["revision"])
                conn.execute(
                    """
                    UPDATE nightly_windows
                    SET completed_revision = MAX(completed_revision, ?),
                        status = CASE
                            WHEN last_revision <= MAX(completed_revision, ?) THEN 'completed'
                            ELSE status
                        END,
                        updated_at_utc = ?
                    WHERE window_id = ?
                    """,
                    (rev, rev, now_utc.isoformat(), win_id),
                )

            cat_rows = conn.execute(
                "SELECT op_type, COUNT(*) AS cnt FROM proactive_applied_ops WHERE job_id = ? GROUP BY op_type",
                (job_id,),
            ).fetchall()
            categories = {str(r["op_type"]): int(r["cnt"]) for r in cat_rows}
            applied_total = sum(categories.values())
            if applied_total == 0 and isinstance(summary, dict):
                applied_total = int(summary.get("applied") or 0)
            receipt_client = display_client_id(str(row["client"] or ("nightly" if row["job_type"] == "nightly" else "")))
            receipt_session = str(row["session_id"] or "")
            conn.execute(
                """
                INSERT INTO review_receipts (
                    job_id, client, session_id, job_type, outcome,
                    applied_count, categories_json, error_summary,
                    created_at_utc, created_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    client = excluded.client,
                    session_id = excluded.session_id,
                    job_type = excluded.job_type,
                    outcome = excluded.outcome,
                    applied_count = excluded.applied_count,
                    categories_json = excluded.categories_json,
                    error_summary = excluded.error_summary,
                    created_at_utc = excluded.created_at_utc,
                    created_at_us = excluded.created_at_us,
                    delivered_at_utc = NULL,
                    delivered_to_client = NULL,
                    delivered_to_session_id = NULL
                """,
                (
                    job_id,
                    receipt_client,
                    receipt_session,
                    str(row["job_type"]),
                    outcome,
                    applied_total,
                    json.dumps(categories, ensure_ascii=False, separators=(",", ":")),
                    now_utc.isoformat(),
                    now_us,
                ),
            )

    def ack_job_failure(
        self,
        job_id: int,
        lease_token: str,
        *,
        error: str,
        retry_delay_seconds: int | None = None,
        force_dead: bool = False,
    ) -> str:
        """记录任务失败并计算指数退避重试时间；绝不推进已评审进度。"""
        now_us = dt_to_us(self.clock.now_utc())
        bounded_err = (error or "unknown_error")[:500]

        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT attempts, max_attempts, job_type, client, session_id
                FROM jobs
                WHERE id = ? AND status = 'running' AND lease_token = ?
                """,
                (job_id, lease_token),
            ).fetchone()
            if row is None:
                raise LeaseMismatchError(f"任务 {job_id} 租约无效或已不在 running 状态")

            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if not force_dead and attempts < max_attempts:
                if retry_delay_seconds is not None:
                    delay_sec = max(0, int(retry_delay_seconds))
                else:
                    delay_sec = min(
                        self.config.max_backoff_seconds,
                        self.config.base_backoff_seconds * (2 ** max(0, attempts - 1)),
                    )
                next_retry_us = now_us + delay_sec * 1_000_000
                new_status = JOB_STATUS_RETRY_WAIT
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        last_error = ?,
                        next_retry_at_us = ?,
                        lease_token = NULL,
                        lease_expires_at_us = NULL
                    WHERE id = ?
                    """,
                    (new_status, bounded_err, next_retry_us, job_id),
                )
            else:
                new_status = JOB_STATUS_DEAD
                now_utc = ensure_utc(self.clock.now_utc())
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        last_error = ?,
                        lease_token = NULL,
                        lease_expires_at_us = NULL,
                        finished_at_us = ?
                    WHERE id = ?
                    """,
                    (new_status, bounded_err, now_us, job_id),
                )
                if new_status == JOB_STATUS_DEAD:
                    # 任务判死后若被重新排队，旧的进度游标会让它“跳过所有分段”直接空跑；
                    # 判死时清掉，保证重跑真的重做。
                    conn.execute(
                        "DELETE FROM adapter_cursors WHERE adapter_name = 'worker_segment' AND source_key = ?",
                        (f"job_segment_progress:{job_id}",),
                    )
                receipt_client = display_client_id(str(row["client"] or ("nightly" if row["job_type"] == "nightly" else "")))
                receipt_session = str(row["session_id"] or "")
                safe_err = redact_secrets(bounded_err)[:160]
                conn.execute(
                    """
                    INSERT INTO review_receipts (
                        job_id, client, session_id, job_type, outcome,
                        applied_count, categories_json, error_summary,
                        created_at_utc, created_at_us
                    ) VALUES (?, ?, ?, ?, 'failed', 0, '{}', ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    client = excluded.client,
                    session_id = excluded.session_id,
                    job_type = excluded.job_type,
                    outcome = excluded.outcome,
                    applied_count = excluded.applied_count,
                    categories_json = excluded.categories_json,
                    error_summary = excluded.error_summary,
                    created_at_utc = excluded.created_at_utc,
                    created_at_us = excluded.created_at_us,
                    delivered_at_utc = NULL,
                    delivered_to_client = NULL,
                    delivered_to_session_id = NULL
                """,
                    (
                        job_id,
                        receipt_client,
                        receipt_session,
                        str(row["job_type"]),
                        safe_err,
                        now_utc.isoformat(),
                        now_us,
                    ),
                )
            return new_status

    def get_activation_baseline_us(self) -> int:
        """读取激活基线时间戳（微秒）。"""
        with self._read() as conn:
            row = conn.execute(
                "SELECT value FROM scheduler_meta WHERE key = 'activation_baseline_us'"
            ).fetchone()
            return int(row["value"]) if row is not None else 0

    def list_pending_receipts(self, *, client_id: str, session_id: str) -> list[dict[str, Any]]:
        """列出当前已鉴权客户端/会话尚未确认送达的评审完成回执。"""
        eff_client = display_client_id(client_id)
        eff_session = str(session_id or "").strip()
        if not eff_client or not eff_session:
            return []
        with self._read() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, client, session_id, job_type, outcome,
                       applied_count, categories_json, error_summary, created_at_utc
                FROM review_receipts
                WHERE delivered_at_utc IS NULL
                  AND (
                      (job_type = 'incremental' AND client = ? AND session_id = ?)
                      OR job_type = 'nightly'
                  )
                ORDER BY id ASC
                """,
                (eff_client, eff_session),
            ).fetchall()
        receipts: list[dict[str, Any]] = []
        for r in rows:
            try:
                cats = json.loads(str(r["categories_json"] or "{}"))
            except json.JSONDecodeError:
                cats = {}
            receipts.append(
                {
                    "id": int(r["id"]),
                    "job_id": int(r["job_id"]),
                    "client": str(r["client"]),
                    "session_id": str(r["session_id"]),
                    "job_type": str(r["job_type"]),
                    "outcome": str(r["outcome"]),
                    "applied_count": int(r["applied_count"]),
                    "categories": cats if isinstance(cats, dict) else {},
                    "error_summary": r["error_summary"],
                    "created_at_utc": str(r["created_at_utc"]),
                }
            )
        return receipts

    def ack_receipts(
        self,
        receipt_ids: list[int],
        *,
        client_id: str,
        session_id: str,
    ) -> int:
        """幂等确认已送达的评审回执（仅允许属主会话或 nightly 回执）。"""
        eff_client = display_client_id(client_id)
        eff_session = str(session_id or "").strip()
        clean_ids = sorted({int(rid) for rid in receipt_ids if int(rid) > 0})
        if not clean_ids or not eff_client or not eff_session:
            return 0
        now_utc = ensure_utc(self.clock.now_utc()).isoformat()
        placeholders = ",".join("?" for _ in clean_ids)
        with self._tx() as conn:
            cur = conn.execute(
                f"""
                UPDATE review_receipts
                SET delivered_at_utc = ?,
                    delivered_to_client = ?,
                    delivered_to_session_id = ?
                WHERE id IN ({placeholders})
                  AND delivered_at_utc IS NULL
                  AND (
                      (job_type = 'incremental' AND client = ? AND session_id = ?)
                      OR job_type = 'nightly'
                  )
                """,
                (now_utc, eff_client, eff_session, *clean_ids, eff_client, eff_session),
            )
            return int(cur.rowcount)

    def get_job_events(self, job_id: int) -> list[StoredEvent]:
        """按任务边界读取关联事件证据（供 Stage 2 worker 使用）。"""
        with self._read() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"未知任务 id: {job_id}")

            if job["job_type"] == "incremental":
                client = str(job["client"])
                session_id = str(job["session_id"])
                b_start = int(job["batch_start"])
                b_end = int(job["batch_end"])
                end_row = conn.execute(
                    """
                    SELECT occurred_at_us FROM events
                    WHERE client = ? AND session_id = ? AND turn_seq = ?
                    """,
                    (client, session_id, b_end),
                ).fetchone()
                end_occ_us = int(end_row["occurred_at_us"]) if end_row is not None else None
                prev_occ_us: int | None = None
                if b_start > 1:
                    prev_row = conn.execute(
                        """
                        SELECT occurred_at_us FROM events
                        WHERE client = ? AND session_id = ? AND turn_seq = ?
                        """,
                        (client, session_id, b_start - 1),
                    ).fetchone()
                    if prev_row is not None:
                        prev_occ_us = int(prev_row["occurred_at_us"])

                rows = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE client = ? AND session_id = ?
                      AND (
                          (countable = 1 AND turn_seq BETWEEN ? AND ?)
                          OR (
                              countable = 0
                              AND (? IS NULL OR occurred_at_us <= ?)
                              AND (? IS NULL OR occurred_at_us > ?)
                          )
                      )
                    ORDER BY occurred_at_us ASC, id ASC
                    """,
                    (
                        client,
                        session_id,
                        b_start,
                        b_end,
                        end_occ_us,
                        end_occ_us,
                        prev_occ_us,
                        prev_occ_us,
                    ),
                ).fetchall()
            else:
                win = conn.execute(
                    """
                    SELECT window_start_us, window_end_us
                    FROM nightly_windows WHERE window_id = ?
                    """,
                    (str(job["window_id"]),),
                ).fetchone()
                if win is None:
                    return []
                rows = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE qualifies_nightly = 1
                      AND occurred_at_us >= ?
                      AND occurred_at_us < ?
                    ORDER BY occurred_at_us ASC, id ASC
                    """,
                    (int(win["window_start_us"]), int(win["window_end_us"])),
                ).fetchall()

            events = [self._row_to_event(r) for r in rows]
            if not events:
                return events
            row_ids = [ev.row_id for ev in events]
            placeholders = ",".join("?" for _ in row_ids)
            chunk_rows = conn.execute(
                f"""
                SELECT event_row_id, content FROM event_chunks
                WHERE event_row_id IN ({placeholders})
                ORDER BY event_row_id ASC, chunk_index ASC
                """,
                row_ids,
            ).fetchall()
            if not chunk_rows:
                return events
            by_row: dict[int, list[str]] = {}
            for cr in chunk_rows:
                by_row.setdefault(int(cr["event_row_id"]), []).append(str(cr["content"]))
            reassembled: list[StoredEvent] = []
            for ev in events:
                parts = by_row.get(ev.row_id)
                if parts:
                    reassembled.append(
                        StoredEvent(
                            row_id=ev.row_id,
                            client=ev.client,
                            session_id=ev.session_id,
                            event_id=ev.event_id,
                            event_kind=ev.event_kind,
                            countable=ev.countable,
                            qualifies_nightly=ev.qualifies_nightly,
                            turn_seq=ev.turn_seq,
                            occurred_at_utc=ev.occurred_at_utc,
                            occurred_at_us=ev.occurred_at_us,
                            content="".join(parts),
                            content_truncated=ev.content_truncated,
                            meta=ev.meta,
                        )
                    )
                else:
                    reassembled.append(ev)
            return reassembled

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            row_id=int(row["id"]),
            client=str(row["client"]),
            session_id=str(row["session_id"]),
            event_id=str(row["event_id"]),
            event_kind=str(row["event_kind"]),
            countable=bool(row["countable"]),
            qualifies_nightly=bool(row["qualifies_nightly"]),
            turn_seq=int(row["turn_seq"]) if row["turn_seq"] is not None else None,
            occurred_at_utc=str(row["occurred_at_utc"]),
            occurred_at_us=int(row["occurred_at_us"]),
            content=str(row["content"]),
            content_truncated=bool(row["content_truncated"]),
            meta=json.loads(str(row["meta_json"])),
        )

    def get_status(self) -> dict[str, Any]:
        """返回脱敏调度状态摘要（绝不包含任何聊天原文 content）。"""
        with self._read() as conn:
            meta_rows = conn.execute("SELECT key, value FROM scheduler_meta").fetchall()
            meta = {str(r["key"]): str(r["value"]) for r in meta_rows}

            counts_by_status = {
                str(r["status"]): int(r["cnt"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
                ).fetchall()
            }
            sessions = [
                {
                    "client": str(r["client"]),
                    "session_id": str(r["session_id"]),
                    "completed_human_turns": int(r["completed_human_turns"]),
                    "last_scheduled_turn": int(r["last_scheduled_turn"]),
                    "last_reviewed_turn": int(r["last_reviewed_turn"]),
                    "updated_at_utc": str(r["updated_at_utc"]),
                }
                for r in conn.execute(
                    "SELECT * FROM session_counters ORDER BY client, session_id"
                ).fetchall()
            ]
            windows = [
                {
                    "window_id": str(r["window_id"]),
                    "window_start_utc": str(r["window_start_utc"]),
                    "window_end_utc": str(r["window_end_utc"]),
                    "offset_start_seconds": int(r["offset_start_seconds"]),
                    "offset_end_seconds": int(r["offset_end_seconds"]),
                    "tz_label": str(r["tz_label"]),
                    "status": str(r["status"]),
                    "last_revision": int(r["last_revision"]),
                    "completed_revision": int(r["completed_revision"]),
                    "watermark_event_id": int(r["watermark_event_id"]),
                }
                for r in conn.execute(
                    "SELECT * FROM nightly_windows ORDER BY window_end_us ASC"
                ).fetchall()
            ]
            jobs: list[dict[str, Any]] = []
            last_model_route: dict[str, Any] | None = None
            for r in conn.execute("SELECT * FROM jobs ORDER BY id ASC").fetchall():
                model_route: dict[str, Any] | None = None
                raw_summary = r["result_summary_json"]
                if raw_summary:
                    try:
                        parsed_summary = json.loads(str(raw_summary))
                        if isinstance(parsed_summary, dict) and isinstance(parsed_summary.get("model_route"), dict):
                            model_route = parsed_summary["model_route"]
                            last_model_route = model_route
                    except json.JSONDecodeError:
                        pass
                job_entry: dict[str, Any] = {
                    "id": int(r["id"]),
                    "idempotency_key": str(r["idempotency_key"]),
                    "job_type": str(r["job_type"]),
                    "status": str(r["status"]),
                    "client": r["client"],
                    "session_id": r["session_id"],
                    "batch_start": r["batch_start"],
                    "batch_end": r["batch_end"],
                    "window_id": r["window_id"],
                    "revision": r["revision"],
                    "event_count": int(r["event_count"]),
                    "attempts": int(r["attempts"]),
                    "max_attempts": int(r["max_attempts"]),
                    "last_error": r["last_error"],
                    "outcome": r["outcome"],
                    "model_route": model_route,
                    "next_retry_at_utc": (
                        us_to_iso(int(r["next_retry_at_us"]))
                        if r["next_retry_at_us"] is not None
                        else None
                    ),
                    "created_at_utc": str(r["created_at_utc"]),
                }
                jobs.append(job_entry)

            return {
                "enabled": self.config.enabled,
                "schema_version": int(meta.get("schema_version", SCHEMA_VERSION)),
                "activation_baseline_utc": meta.get("activation_baseline_utc"),
                "baseline_last_closed_date": meta.get("baseline_last_closed_date"),
                "last_model_route": last_model_route,
                "job_counts": counts_by_status,
                "sessions": sessions,
                "nightly_windows": windows,
                "jobs": jobs,
            }

    def prune_events(self, *, retention_days: int | None = None) -> int:
        """清理超过保留期且已被所有相关任务安全消费的事件；绝不删除待处理证据。"""
        days = retention_days if retention_days is not None else self.config.retention_days
        cutoff_us = dt_to_us(self.clock.now_utc() - dt.timedelta(days=days))
        with self._tx() as conn:
            # 1. 若存在任何未完成夜间窗口，禁止删除落在该窗口内的事件
            # 2. 若任何未完成增量批次尚未覆盖某事件，禁止删除
            # 3. 若某事件发生晚于所有已关闭夜间窗口的结束时间，禁止删除（可能属于未关闭窗口）
            cur = conn.execute(
                """
                DELETE FROM events
                WHERE occurred_at_us < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j
                      WHERE j.job_type = 'incremental'
                        AND j.status IN ('pending', 'running', 'retry_wait')
                        AND j.client = events.client
                        AND j.session_id = events.session_id
                        AND (events.turn_seq IS NULL OR events.turn_seq <= j.batch_end)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM session_counters sc
                      WHERE sc.client = events.client
                        AND sc.session_id = events.session_id
                        AND events.countable = 1
                        AND events.turn_seq > sc.last_reviewed_turn
                  )
                  AND EXISTS (
                      SELECT 1 FROM nightly_windows nw
                      WHERE events.occurred_at_us >= nw.window_start_us
                        AND events.occurred_at_us < nw.window_end_us
                        AND nw.status IN ('skipped_empty', 'completed')
                        AND nw.last_revision = nw.completed_revision
                  )
                """,
                (cutoff_us,),
            )
            return cur.rowcount

    def try_acquire_runner_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "proactive_runner",
        lease_seconds: int = 60,
    ) -> str | None:
        """尝试获取单写者进程级耐久租约；若仍被其他存活实例持有则返回 None。"""
        now_us = dt_to_us(self.clock.now_utc())
        expires_us = now_us + max(1, int(lease_seconds)) * 1_000_000
        token = uuid.uuid4().hex
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT owner_id, lease_token, expires_at_us FROM scheduler_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
            if existing is not None and int(existing["expires_at_us"]) > now_us:
                if str(existing["owner_id"]) != owner_id and _owner_process_alive(str(existing["owner_id"])):
                    return None
            conn.execute(
                """
                INSERT INTO scheduler_leases (
                    lease_name, owner_id, lease_token, acquired_at_us, expires_at_us
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    lease_token = excluded.lease_token,
                    acquired_at_us = excluded.acquired_at_us,
                    expires_at_us = excluded.expires_at_us
                """,
                (lease_name, owner_id, token, now_us, expires_us),
            )
            return token

    def renew_runner_lease(
        self,
        owner_id: str,
        lease_token: str,
        *,
        lease_name: str = "proactive_runner",
        lease_seconds: int = 60,
    ) -> bool:
        """续约进程级耐久租约；若租约已失则返回 False。"""
        now_us = dt_to_us(self.clock.now_utc())
        expires_us = now_us + max(1, int(lease_seconds)) * 1_000_000
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE scheduler_leases
                SET expires_at_us = ?
                WHERE lease_name = ? AND owner_id = ? AND lease_token = ? AND expires_at_us > ?
                """,
                (expires_us, lease_name, owner_id, lease_token, now_us),
            )
            return cur.rowcount > 0

    def release_runner_lease(
        self,
        owner_id: str,
        lease_token: str,
        *,
        lease_name: str = "proactive_runner",
    ) -> None:
        """释放进程级耐久租约。"""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM scheduler_leases WHERE lease_name = ? AND owner_id = ? AND lease_token = ?",
                (lease_name, owner_id, lease_token),
            )

    def get_adapter_cursor(self, adapter_name: str, source_key: str) -> dict[str, Any] | None:
        """读取适配器持久化游标。"""
        with self._read() as conn:
            row = conn.execute(
                "SELECT cursor_json FROM adapter_cursors WHERE adapter_name = ? AND source_key = ?",
                (adapter_name, source_key),
            ).fetchone()
            if row is None:
                return None
            parsed = json.loads(str(row["cursor_json"]))
            return parsed if isinstance(parsed, dict) else None

    def set_adapter_cursor(
        self,
        adapter_name: str,
        source_key: str,
        cursor: dict[str, Any],
    ) -> None:
        """持久化适配器游标。"""
        now_iso = ensure_utc(self.clock.now_utc()).isoformat()
        encoded = json.dumps(cursor, ensure_ascii=False, separators=(",", ":"))
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO adapter_cursors (adapter_name, source_key, cursor_json, updated_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(adapter_name, source_key) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (adapter_name, source_key, encoded, now_iso),
            )

    def list_adapter_cursors(self, adapter_name: str | None = None) -> list[dict[str, Any]]:
        """列出适配器游标元信息（不含聊天原文）。"""
        with self._read() as conn:
            if adapter_name:
                rows = conn.execute(
                    "SELECT adapter_name, source_key, cursor_json, updated_at_utc "
                    "FROM adapter_cursors WHERE adapter_name = ? ORDER BY source_key",
                    (adapter_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT adapter_name, source_key, cursor_json, updated_at_utc "
                    "FROM adapter_cursors ORDER BY adapter_name, source_key"
                ).fetchall()
            result: list[dict[str, Any]] = []
            for r in rows:
                cursor_obj = json.loads(str(r["cursor_json"]))
                safe_cursor = {
                    k: v
                    for k, v in cursor_obj.items()
                    if k not in ("pending_user_content", "pending_user_turn", "content", "raw", "segments_prompts")
                }
                if "pending_user_turn" in cursor_obj:
                    safe_cursor["has_pending_user"] = cursor_obj.get("pending_user_turn") is not None
                result.append(
                    {
                        "adapter_name": str(r["adapter_name"]),
                        "source_key": str(r["source_key"]),
                        "cursor": safe_cursor,
                        "updated_at_utc": str(r["updated_at_utc"]),
                    }
                )
            return result

    def is_op_applied(
        self,
        op_id: str,
        op_type: str | None = None,
        dedupe_key: str | None = None,
        *,
        check_dedupe: bool = False,
    ) -> bool:
        """检查操作是否已执行；默认仅按 op_id（避免全局内容去重误杀状态回退），event 类可启用 check_dedupe。"""
        with self._read() as conn:
            if check_dedupe and op_type is not None and dedupe_key is not None:
                row = conn.execute(
                    """
                    SELECT 1 FROM proactive_applied_ops
                    WHERE op_id = ? OR (op_type = ? AND dedupe_key = ?)
                    LIMIT 1
                    """,
                    (op_id, op_type, dedupe_key),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM proactive_applied_ops WHERE op_id = ? LIMIT 1",
                    (op_id,),
                ).fetchone()
            return row is not None

    def record_applied_op(
        self,
        *,
        op_id: str,
        job_id: int,
        job_idempotency_key: str,
        op_type: str,
        dedupe_key: str,
        target_path: str,
        target_date: str | None = None,
        source_refs: list[str] | None = None,
    ) -> None:
        """记录已应用操作，支持跨增量/夜间任务的语义槽位与来源引用去重。"""
        now_iso = ensure_utc(self.clock.now_utc()).isoformat()
        refs_json = json.dumps(source_refs or [], ensure_ascii=False, separators=(",", ":"))
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO proactive_applied_ops (
                    op_id, job_id, job_idempotency_key, op_type, dedupe_key,
                    target_path, target_date, source_refs_json, applied_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    op_id,
                    job_id,
                    job_idempotency_key,
                    op_type,
                    dedupe_key,
                    target_path,
                    target_date,
                    refs_json,
                    now_iso,
                ),
            )

    def reconcile_countable_events(self, *, force: bool = False) -> dict[str, Any]:
        """一次性幂等收敛：按新可计数口径重算 countable/qualifies_nightly/completed_human_turns，水位只升不降。"""
        now_iso = ensure_utc(self.clock.now_utc()).isoformat()
        meta_key = "countable_classification_v1_migrated"

        with self._tx() as conn:
            if not force:
                done = conn.execute(
                    "SELECT value FROM scheduler_meta WHERE key = ?",
                    (meta_key,),
                ).fetchone()
                if done is not None:
                    return {
                        "already_migrated": True,
                        "events_scanned": 0,
                        "demoted_to_uncountable": 0,
                        "sessions_updated": 0,
                        "total_countable_turns": 0,
                    }

            rows = conn.execute(
                """
                SELECT id, client, session_id, countable, qualifies_nightly, turn_seq, content, meta_json
                FROM events
                ORDER BY client ASC, session_id ASC, occurred_at_us ASC, id ASC
                """
            ).fetchall()

            events_scanned = len(rows)
            demoted = 0
            session_seq: dict[tuple[str, str], int] = {}
            countable_records: list[tuple[tuple[str, str], int, int | None]] = []

            for r in rows:
                row_id = int(r["id"])
                key = (str(r["client"]), str(r["session_id"]))
                cur_countable = int(r["countable"])
                cur_nightly = int(r["qualifies_nightly"])
                cur_seq = r["turn_seq"]
                turn_kind = classify_user_turn(str(r["content"] or ""))

                try:
                    meta_obj = json.loads(str(r["meta_json"] or "{}"))
                    if not isinstance(meta_obj, dict):
                        meta_obj = {}
                except json.JSONDecodeError:
                    meta_obj = {}

                if turn_kind == "system_notice":
                    if cur_countable == 1:
                        demoted += 1
                    new_countable = 0
                    new_nightly = 0
                    new_seq: int | None = None
                    if (
                        cur_countable != 0
                        or cur_nightly != 0
                        or cur_seq is not None
                        or meta_obj.get("turn_kind") != "system_notice"
                    ):
                        meta_obj["turn_kind"] = "system_notice"
                        conn.execute(
                            """
                            UPDATE events
                            SET countable = 0, qualifies_nightly = 0, turn_seq = NULL, meta_json = ?
                            WHERE id = ?
                            """,
                            (_bound_meta(meta_obj), row_id),
                        )
                else:
                    new_countable = cur_countable
                    new_seq = None
                    if new_countable == 1:
                        next_seq = session_seq.get(key, 0) + 1
                        session_seq[key] = next_seq
                        new_seq = next_seq
                        countable_records.append((key, row_id, int(cur_seq) if cur_seq is not None else None))
                    if cur_seq != new_seq or meta_obj.get("turn_kind") != "user":
                        meta_obj["turn_kind"] = "user"
                        conn.execute(
                            """
                            UPDATE events
                            SET turn_seq = ?, meta_json = ?
                            WHERE id = ?
                            """,
                            (new_seq, _bound_meta(meta_obj), row_id),
                        )

            # 已排队但仍未跑完的任务：这些会话的水位不能动，否则会重复排批或漏批。
            open_sessions: set[tuple[str, str]] = set()
            # 增量任务记录的 batch_end 是「旧编号」的轮次水位；夜间任务是全局事件序号。
            session_old_end: dict[tuple[str, str], int] = {}
            nightly_until = 0
            for r in conn.execute(
                "SELECT client, session_id, status, batch_end, window_id, until_event_id FROM jobs"
            ):
                key = (str(r["client"]), str(r["session_id"] or ""))
                if str(r["status"]) in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JOB_STATUS_RETRY_WAIT):
                    open_sessions.add(key)
                    continue
                if str(r["status"]) != "succeeded":
                    continue
                if r["window_id"]:
                    try:
                        nightly_until = max(nightly_until, int(r["until_event_id"] or 0))
                    except (TypeError, ValueError):
                        pass
                    continue
                if r["batch_end"] is not None:
                    session_old_end[key] = max(session_old_end.get(key, 0), int(r["batch_end"]))

            # 旧编号 -> 新编号的可计数轮次：按「旧 turn_seq 是否已落在已跑批次内」换算。
            reviewed_by_session: dict[tuple[str, str], int] = {}
            global_ordinal: dict[int, int] = {}
            for idx, r in enumerate(
                conn.execute("SELECT id FROM events ORDER BY occurred_at_us ASC, id ASC"), start=1
            ):
                global_ordinal[int(r["id"])] = idx

            for key, row_id, old_seq in countable_records:
                covered = old_seq is not None and old_seq <= session_old_end.get(key, 0)
                if not covered and nightly_until:
                    covered = global_ordinal.get(row_id, 0) <= nightly_until
                if covered:
                    reviewed_by_session[key] = reviewed_by_session.get(key, 0) + 1

            existing_counters = conn.execute(
                "SELECT client, session_id FROM session_counters"
            ).fetchall()
            all_sessions = set(session_seq.keys()) | {
                (str(r["client"]), str(r["session_id"])) for r in existing_counters
            }

            sessions_updated = 0
            total_countable_turns = 0
            for client, session_id in sorted(all_sessions):
                new_completed = session_seq.get((client, session_id), 0)
                total_countable_turns += new_completed
                conn.execute(
                    """
                    INSERT OR IGNORE INTO session_counters (
                        client, session_id, completed_human_turns,
                        last_scheduled_turn, last_reviewed_turn, updated_at_utc
                    ) VALUES (?, ?, 0, 0, 0, ?)
                    """,
                    (client, session_id, now_iso),
                )
                if (client, session_id) in open_sessions:
                    conn.execute(
                        """
                        UPDATE session_counters
                        SET completed_human_turns = ?, updated_at_utc = ?
                        WHERE client = ? AND session_id = ?
                        """,
                        (new_completed, now_iso, client, session_id),
                    )
                else:
                    new_reviewed = min(new_completed, reviewed_by_session.get((client, session_id), 0))
                    conn.execute(
                        """
                        UPDATE session_counters
                        SET completed_human_turns = ?,
                            last_scheduled_turn = ?,
                            last_reviewed_turn = ?,
                            updated_at_utc = ?
                        WHERE client = ? AND session_id = ?
                        """,
                        (new_completed, new_reviewed, new_reviewed, now_iso, client, session_id),
                    )
                sessions_updated += 1

            conn.execute(
                "INSERT OR REPLACE INTO scheduler_meta (key, value) VALUES (?, ?)",
                (meta_key, now_iso),
            )

            return {
                "already_migrated": False,
                "events_scanned": events_scanned,
                "demoted_to_uncountable": demoted,
                "sessions_updated": sessions_updated,
                "total_countable_turns": total_countable_turns,
            }

    def record_archive_log(
        self,
        *,
        note_path: str,
        archived_path: str,
        mtime_us: int,
        last_access_us: int,
        archived_at_utc: str,
        archive_version: int = 1,
    ) -> None:
        """写入一条归档审计记录到 archive_log 表。"""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO archive_log (
                    note_path, archived_path, mtime_us, last_access_us, archived_at_utc, archive_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    note_path,
                    archived_path,
                    int(mtime_us),
                    int(last_access_us),
                    archived_at_utc,
                    int(archive_version),
                ),
            )

    def record_archive_failed_receipt(
        self,
        *,
        job_id: int,
        error: str,
    ) -> None:
        """单独记录归档失败回执，不覆盖已成功复盘的回执与任务状态。"""
        now_utc = ensure_utc(self.clock.now_utc())
        now_us = dt_to_us(now_utc)
        safe_err = redact_secrets(str(error or "archive_error"))[:160]
        receipt_job_id = -abs(int(job_id)) if int(job_id) != 0 else -1
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO review_receipts (
                    job_id, client, session_id, job_type, outcome,
                    applied_count, categories_json, error_summary,
                    created_at_utc, created_at_us
                ) VALUES (?, 'nightly', '', 'nightly', 'archive_failed', 0, '{}', ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    outcome = excluded.outcome,
                    error_summary = excluded.error_summary,
                    created_at_utc = excluded.created_at_utc,
                    created_at_us = excluded.created_at_us,
                    delivered_at_utc = NULL,
                    delivered_to_client = NULL,
                    delivered_to_session_id = NULL
                """,
                (receipt_job_id, safe_err, now_utc.isoformat(), now_us),
            )
