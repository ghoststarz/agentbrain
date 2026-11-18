"""Stage 2 数据源适配器：agy IDE transcript JSONL 增量扫描与 agy CLI 引用事件采集。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbrain.proactive import (
    EVENT_KIND_HUMAN_TURN,
    EVENT_KIND_REFERENCE_ONLY,
    ProactiveStore,
    classify_user_turn,
    normalize_user_text,
)
from agentbrain.proactive_redaction import redact_secrets


@dataclass(frozen=True)
class IdeScanReport:
    """一次 agy IDE 目录扫描的结构化诊断（不含聊天原文）。"""

    roots_scanned: int
    transcripts_scanned: int
    events_ingested: int
    turns_completed: int
    unavailable_sources: list[dict[str, str]]
    skipped_duplicate: int = 0


@dataclass(frozen=True)
class HermesStateDbScanReport:
    """一次 Hermes state.db 只读增量扫描的结构化诊断（不含聊天原文）。"""

    db_path: str
    available: bool
    sessions_scanned: int
    turns_completed: int
    events_ingested: int
    events_deduped: int
    skipped_pre_activation: int
    skipped_excluded: int
    last_error: str | None = None
    skipped_duplicate: int = 0


_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_INJECTED_BLOCK_RE = re.compile(
    r"(?:\[SHARED_BRAIN[^\]]*\][\s\S]*?(?=\n\n|\Z)|"
    r"\[MEMORY_DELTA\][^\n]*\n?|"
    r"\[MEMORY_PREFETCH\][\s\S]*?(?=\n\n|\Z)|"
    r"\[PREVIOUS_REVIEW_RESULT\][^\n]*\n?|"
    r"\[DIARY\][^\n]*\n?)"
)

_EXCLUDED_HERMES_SOURCES = frozenset({"cron", "subagent", "oneshot", "system", "heartbeat", "review"})
_EXCLUDED_HERMES_SESSION_PREFIXES = ("proactive-review", "system", "heartbeat", "cron", "__internal")

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_BG_PROC_TOKEN_RE = re.compile(r"\b(?:proc|task|job|bg)[_-][a-z0-9_-]+\b", re.IGNORECASE)
_BG_KV_ID_RE = re.compile(
    r"\b(?:pid|process_id|task_id|job_id)\s*[:=]\s*[a-z0-9_-]+\b",
    re.IGNORECASE,
)
_BG_PROCESS_NUM_RE = re.compile(r"\bprocess\s+\d+\b", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t]{2,}")
_NEAR_DEDUPE_WINDOW_SECONDS = 120.0


def _normalize_for_near_dedupe(text: str) -> str:
    """归一化正文并剥离后台进程/任务 ID，用于同会话 120s 相邻近重复去重。"""
    norm = normalize_user_text(text)
    if not norm:
        return ""
    norm = _UUID_RE.sub("<ID>", norm)
    norm = _BG_KV_ID_RE.sub("<ID>", norm)
    norm = _BG_PROC_TOKEN_RE.sub("<ID>", norm)
    norm = _BG_PROCESS_NUM_RE.sub("process <ID>", norm)
    return _SPACES_RE.sub(" ", norm)


def _is_near_duplicate(
    prev: dict[str, Any] | None,
    eff_ts: float,
    norm_hash: str,
) -> bool:
    if not isinstance(prev, dict) or eff_ts <= 0:
        return False
    prev_ts = float(prev.get("ts") or 0.0)
    if prev_ts <= 0:
        return False
    return abs(eff_ts - prev_ts) <= _NEAR_DEDUPE_WINDOW_SECONDS and prev.get("hash") == norm_hash


def _clean_hermes_text(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", text or "")
    cleaned = _INJECTED_BLOCK_RE.sub("", cleaned)
    return redact_secrets(cleaned).strip()


def _parse_utc_iso(raw: Any) -> dt.datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _is_internal_review_noise(text: str, meta: dict[str, Any] | None = None) -> bool:
    if meta and meta.get("proactive_review_internal") is True:
        return True
    lower = text.lower()
    return "<untrusted_evidence>" in lower or "proactive_stage2_internal" in lower


def _compute_file_prefix(jsonl_path: Path, max_bytes: int = 512) -> tuple[int, str]:
    """计算文件前 max_bytes 字节的指纹，用于检测同大小或更大文件的替换/重写。"""
    if max_bytes <= 0:
        return 0, ""
    with jsonl_path.open("rb") as fh:
        data = fh.read(max_bytes)
    if not data:
        return 0, ""
    return len(data), hashlib.sha256(data).hexdigest()


def _scan_single_transcript(
    store: ProactiveStore,
    conv_id: str,
    jsonl_path: Path,
    *,
    catchup: bool = False,
    is_initial_baseline: bool = False,
    root_tag: str = "",
) -> tuple[int, int, int]:
    """增量扫描单个 transcript.jsonl，支持源级首次基线、部分行、同大小替换/截断与未完成轮次保留。"""
    source_key = str(jsonl_path.resolve())
    cursor = store.get_adapter_cursor("agy_ide", source_key)
    file_size = jsonl_path.stat().st_size
    if not root_tag:
        root_tag = hashlib.sha256(str(jsonl_path.parent.parent.parent.resolve()).encode("utf-8")).hexdigest()[:6]

    if cursor is None and is_initial_baseline and not catchup:
        # 源级首次激活基线：仅尾随已有完整行历史，保留部分行待续及未完成用户轮次
        last_step = -1
        last_complete_offset = 0
        pending_user: dict[str, Any] | None = None
        with jsonl_path.open("rb") as fh:
            while True:
                line_start = fh.tell()
                raw_line = fh.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    last_complete_offset = line_start
                    break
                try:
                    obj = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    last_complete_offset = line_start
                    break
                last_complete_offset = fh.tell()
                if not isinstance(obj, dict):
                    continue
                step_index = obj.get("step_index")
                if isinstance(step_index, int):
                    last_step = max(last_step, int(step_index))
                source = str(obj.get("source") or "").strip()
                ev_type = str(obj.get("type") or "").strip()
                status = str(obj.get("status") or "").strip()
                content = str(obj.get("content") or "")
                tool_calls = obj.get("tool_calls")
                has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0
                if not _is_internal_review_noise(content):
                    if source == "USER_EXPLICIT" and ev_type == "USER_INPUT" and status == "DONE" and content.strip():
                        pending_user = {
                            "step_index": int(step_index) if isinstance(step_index, int) else -1,
                            "created_at": obj.get("created_at"),
                            "content": redact_secrets(content),
                        }
                    elif (
                        source == "MODEL"
                        and ev_type == "PLANNER_RESPONSE"
                        and status == "DONE"
                        and content.strip()
                        and not has_tools
                    ):
                        pending_user = None
        prefix_len, prefix_hash = _compute_file_prefix(jsonl_path, min(last_complete_offset, 512))
        store.set_adapter_cursor(
            "agy_ide",
            source_key,
            {
                "conv_id": conv_id,
                "root_tag": root_tag,
                "byte_offset": last_complete_offset,
                "last_step_index": last_step,
                "generation": 0,
                "prefix_len": prefix_len,
                "prefix_hash": prefix_hash,
                "pending_user_turn": pending_user,
            },
        )
        return 0, 0, 0

    if cursor is None:
        # 激活后新建的对话文件或显式 catchup：从头处理
        byte_offset = 0
        last_step = -1
        generation = 0
        pending_user = None
        last_event: dict[str, Any] | None = None
    else:
        byte_offset = int(cursor.get("byte_offset", 0))
        last_step = int(cursor.get("last_step_index", -1))
        generation = int(cursor.get("generation", 0))
        pending_user = cursor.get("pending_user_turn")
        last_event = cursor.get("last_event") if isinstance(cursor.get("last_event"), dict) else None
        if isinstance(pending_user, dict) and "content" in pending_user:
            pending_user = {
                **pending_user,
                "content": redact_secrets(str(pending_user.get("content") or "")),
            }
        prev_prefix_len = int(cursor.get("prefix_len") or 0)
        prev_prefix_hash = str(cursor.get("prefix_hash") or "")
        replaced = file_size < byte_offset
        if not replaced and prev_prefix_len > 0:
            cur_len_actual, cur_hash_actual = _compute_file_prefix(jsonl_path, prev_prefix_len)
            if cur_len_actual < prev_prefix_len or cur_hash_actual != prev_prefix_hash:
                replaced = True
        if replaced:
            # 文件被截断、重写或同大小替换：回到开头，重置 step 游标并递增 generation，保留未完成的用户轮次
            byte_offset = 0
            last_step = -1
            generation += 1

    ingested = 0
    turns = 0
    skipped_dup = 0
    new_offset = byte_offset

    with jsonl_path.open("rb") as fh:
        fh.seek(byte_offset)
        while True:
            line_start = fh.tell()
            raw_line = fh.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                # 忽略尚未写完的部分 JSON 行，等下次轮询再读
                new_offset = line_start
                break
            try:
                obj = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                new_offset = line_start
                break

            new_offset = fh.tell()
            if not isinstance(obj, dict):
                continue

            step_index = obj.get("step_index")
            if not isinstance(step_index, int) or step_index <= last_step:
                continue
            last_step = step_index

            source = str(obj.get("source") or "").strip()
            ev_type = str(obj.get("type") or "").strip()
            status = str(obj.get("status") or "").strip()
            content = str(obj.get("content") or "")
            tool_calls = obj.get("tool_calls")
            has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0
            occ_dt = _parse_utc_iso(obj.get("created_at"))

            if _is_internal_review_noise(content):
                continue

            if source == "USER_EXPLICIT" and ev_type == "USER_INPUT" and status == "DONE":
                if content.strip():
                    pending_user = {
                        "step_index": step_index,
                        "created_at": obj.get("created_at"),
                        "content": redact_secrets(content),
                    }
                continue

            if source == "GENERIC" or ev_type == "GENERIC" or (source == "MODEL" and has_tools):
                tool_summary = content.strip()
                if has_tools:
                    tool_names = [
                        str(tc.get("name") or tc.get("function", {}).get("name") or "tool")
                        for tc in tool_calls
                        if isinstance(tc, dict)
                    ]
                    tool_summary = f"tool_calls={','.join(tool_names)} {tool_summary}".strip()
                if tool_summary:
                    gen_suffix = f":g{generation}" if generation > 0 else ""
                    res = store.ingest_event(
                        client="agy_ide",
                        session_id=f"{root_tag}:{conv_id}",
                        event_id=f"agy_ide:{root_tag}:{conv_id}{gen_suffix}:step:{step_index}",
                        event_kind=EVENT_KIND_REFERENCE_ONLY,
                        content=tool_summary,
                        occurred_at=occ_dt,
                        meta={"step_index": step_index, "source": source, "type": ev_type, "conv_id": conv_id},
                        qualifies_nightly=True,
                    )
                    if res.accepted and not res.deduped:
                        ingested += 1
                continue

            if (
                source == "MODEL"
                and ev_type == "PLANNER_RESPONSE"
                and status == "DONE"
                and content.strip()
                and not has_tools
            ):
                if pending_user is not None:
                    u_step = int(pending_user["step_index"])
                    user_text = pending_user["content"].strip()
                    turn_kind = classify_user_turn(user_text)
                    turn_text = f"User: {user_text}\nAssistant: {content.strip()}"
                    eff_ts = occ_dt.timestamp() if occ_dt is not None else 0.0
                    norm_hash = hashlib.sha256(_normalize_for_near_dedupe(turn_text).encode("utf-8")).hexdigest()
                    if turn_kind == "system_notice" and _is_near_duplicate(last_event, eff_ts, norm_hash):
                        skipped_dup += 1
                        pending_user = None
                        continue

                    if turn_kind == "system_notice":
                        ev_kind = EVENT_KIND_REFERENCE_ONLY
                        qualifies_nightly = False
                    else:
                        ev_kind = EVENT_KIND_HUMAN_TURN
                        qualifies_nightly = True

                    gen_suffix = f":g{generation}" if generation > 0 else ""
                    res = store.ingest_event(
                        client="agy_ide",
                        session_id=f"{root_tag}:{conv_id}",
                        event_id=f"agy_ide:{root_tag}:{conv_id}{gen_suffix}:turn:{u_step}-{step_index}",
                        event_kind=ev_kind,
                        content=turn_text,
                        occurred_at=occ_dt,
                        meta={
                            "user_step": u_step,
                            "model_step": step_index,
                            "conv_id": conv_id,
                            "turn_kind": turn_kind,
                        },
                        qualifies_nightly=qualifies_nightly,
                    )
                    if res.accepted and not res.deduped:
                        ingested += 1
                        if ev_kind == EVENT_KIND_HUMAN_TURN:
                            turns += 1
                    if eff_ts > 0:
                        last_event = {"ts": eff_ts, "hash": norm_hash}
                    pending_user = None

    prefix_len, prefix_hash = _compute_file_prefix(jsonl_path, min(new_offset, 512))
    store.set_adapter_cursor(
        "agy_ide",
        source_key,
        {
            "conv_id": conv_id,
            "root_tag": root_tag,
            "byte_offset": new_offset,
            "last_step_index": last_step,
            "generation": generation,
            "prefix_len": prefix_len,
            "prefix_hash": prefix_hash,
            "pending_user_turn": pending_user,
            "last_event": last_event,
        },
    )
    return ingested, turns, skipped_dup


def scan_agy_ide_roots(
    store: ProactiveStore,
    roots: tuple[Path, ...] | list[Path],
    *,
    catchup: bool = False,
) -> IdeScanReport:
    """扫描显式配置的 agy_ide_roots；缺失 JSONL 时可见标记不可用，绝不解析 protobuf。"""
    baseline_cursor = store.get_adapter_cursor("agy_ide_baseline", "global")
    is_initial_baseline = baseline_cursor is None

    roots_scanned = 0
    transcripts_scanned = 0
    events_ingested = 0
    turns_completed = 0
    skipped_duplicate = 0
    unavailable: list[dict[str, str]] = []

    for root in roots:
        if not root.exists() or not root.is_dir():
            unavailable.append({"root": str(root), "reason": "root_missing"})
            continue
        roots_scanned += 1
        root_tag = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:6]
        for conv_dir in sorted(root.iterdir()):
            if not conv_dir.is_dir() or conv_dir.name.startswith("."):
                continue
            jsonl_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not jsonl_path.exists():
                unavailable.append({
                    "conv_id": conv_dir.name,
                    "reason": "source_unavailable_no_jsonl",
                })
                continue
            transcripts_scanned += 1
            ing, trn, dup = _scan_single_transcript(
                store,
                conv_dir.name,
                jsonl_path,
                catchup=catchup,
                is_initial_baseline=is_initial_baseline,
                root_tag=root_tag,
            )
            events_ingested += ing
            turns_completed += trn
            skipped_duplicate += dup

    if is_initial_baseline:
        store.set_adapter_cursor(
            "agy_ide_baseline",
            "global",
            {
                "initialized": True,
                "activated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )

    return IdeScanReport(
        roots_scanned=roots_scanned,
        transcripts_scanned=transcripts_scanned,
        events_ingested=events_ingested,
        turns_completed=turns_completed,
        unavailable_sources=unavailable,
        skipped_duplicate=skipped_duplicate,
    )


def ingest_cli_reference_event(
    store: ProactiveStore,
    *,
    session_id: str,
    content: str,
    event_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """显式采集 agy CLI 引用事件（不计入人类完成轮次）。"""
    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("CLI reference event content 不能为空")
    eid = (event_id or "").strip()
    if not eid:
        digest = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()[:16]
        eid = f"agy_cli:{session_id}:{digest}"
    res = store.ingest_event(
        client="agy_cli",
        session_id=session_id,
        event_id=eid,
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content=clean_content,
        meta=meta or {"source": "cli_reference"},
        qualifies_nightly=True,
    )
    return {
        "accepted": res.accepted,
        "deduped": res.deduped,
        "event_row_id": res.event_row_id,
        "completed_human_turns": res.completed_human_turns,
    }


def resolve_hermes_statedb_path(config: Any) -> Path | None:
    """解析 Hermes 规范 state.db 路径（配置显式覆盖优先，其次按 profile home 推导）。"""
    pro_cfg = config.memory.proactive
    if not pro_cfg.hermes_statedb_enabled:
        return None
    explicit = str(getattr(pro_cfg, "hermes_state_db_path", "") or "").strip()
    if explicit:
        return Path(explicit).resolve()
    try:
        from agentbrain.proactive_hermes import resolve_hermes_profile_home

        _, profile_home = resolve_hermes_profile_home(config)
        return (profile_home / "state.db").resolve()
    except Exception:
        return None


def _is_excluded_hermes_session(session_id: str, source: str, hidden: int) -> bool:
    if int(hidden or 0) == 1:
        return True
    sid_low = (session_id or "").strip().lower()
    if not sid_low or sid_low.startswith(_EXCLUDED_HERMES_SESSION_PREFIXES):
        return True
    src_low = (source or "").strip().lower()
    return src_low in _EXCLUDED_HERMES_SOURCES


def scan_hermes_statedb(
    store: ProactiveStore,
    db_path: Path,
    *,
    catchup: bool = False,
) -> HermesStateDbScanReport:
    """只读增量扫描 Hermes 规范 state.db，采集激活基线之后的已完成轮次并与 provider 路由去重。"""
    resolved = Path(db_path).resolve()
    db_str = str(resolved)
    if not resolved.exists() or not resolved.is_file():
        return HermesStateDbScanReport(
            db_path=db_str,
            available=False,
            sessions_scanned=0,
            turns_completed=0,
            events_ingested=0,
            events_deduped=0,
            skipped_pre_activation=0,
            skipped_excluded=0,
            last_error="state_db_missing",
        )

    baseline_us = store.get_activation_baseline_us()
    baseline_epoch = baseline_us / 1_000_000.0 if baseline_us > 0 else 0.0
    cursor = store.get_adapter_cursor("hermes_statedb", db_str)
    last_msg_id = int(cursor.get("last_msg_id", 0)) if isinstance(cursor, dict) else 0
    raw_pending = cursor.get("pending_users") if isinstance(cursor, dict) else None
    pending_users: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in raw_pending.items() if isinstance(v, dict)}
        if isinstance(raw_pending, dict)
        else {}
    )

    raw_last_by_session = cursor.get("last_by_session") if isinstance(cursor, dict) else None
    last_by_session: dict[str, dict[str, Any]] = (
        {str(k): dict(v) for k, v in raw_last_by_session.items() if isinstance(v, dict)}
        if isinstance(raw_last_by_session, dict)
        else {}
    )

    sessions_seen: set[str] = set()
    turns_completed = 0
    events_ingested = 0
    events_deduped = 0
    skipped_pre_activation = 0
    skipped_excluded = 0
    skipped_duplicate = 0

    try:
        uri = f"file:{resolved.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except Exception as exc:  # noqa: BLE001
        return HermesStateDbScanReport(
            db_path=db_str,
            available=False,
            sessions_scanned=0,
            turns_completed=0,
            events_ingested=0,
            events_deduped=0,
            skipped_pre_activation=0,
            skipped_excluded=0,
            last_error=redact_secrets(f"{type(exc).__name__}: {exc}")[:200],
        )

    try:
        if not catchup and baseline_epoch > 0:
            pre_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE id > ? AND timestamp IS NOT NULL AND timestamp < ?",
                (last_msg_id, baseline_epoch),
            ).fetchone()
            skipped_pre_activation = int(pre_row["cnt"]) if pre_row is not None else 0

        query_ts = 0.0 if catchup else baseline_epoch
        rows = conn.execute(
            """
            SELECT m.id, m.session_id, m.role, m.content, m.finish_reason,
                   m.tool_calls, m.timestamp, m.message_uid,
                   s.source AS session_source, COALESCE(s.hidden, 0) AS session_hidden
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.id > ? AND (m.timestamp IS NULL OR m.timestamp >= ?)
            ORDER BY m.id ASC
            """,
            (last_msg_id, query_ts),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        conn.close()
        return HermesStateDbScanReport(
            db_path=db_str,
            available=False,
            sessions_scanned=0,
            turns_completed=0,
            events_ingested=0,
            events_deduped=0,
            skipped_pre_activation=skipped_pre_activation,
            skipped_excluded=0,
            last_error=redact_secrets(f"{type(exc).__name__}: {exc}")[:200],
        )
    conn.close()

    new_last_id = last_msg_id
    for r in rows:
        row_id = int(r["id"])
        new_last_id = max(new_last_id, row_id)
        sid = str(r["session_id"] or "").strip()
        src = str(r["session_source"] or "").strip().lower()
        hidden = int(r["session_hidden"] or 0)

        if _is_excluded_hermes_session(sid, src, hidden):
            skipped_excluded += 1
            pending_users.pop(sid, None)
            continue

        sessions_seen.add(sid)
        role = str(r["role"] or "").strip().lower()
        raw_content = str(r["content"] or "")
        if _is_internal_review_noise(raw_content):
            skipped_excluded += 1
            continue

        clean_text = _clean_hermes_text(raw_content)
        ts_val = r["timestamp"]
        uid = str(r["message_uid"] or "").strip()

        if role == "user":
            if clean_text:
                pending_users[sid] = {
                    "id": row_id,
                    "uid": uid,
                    "ts": float(ts_val) if ts_val is not None else 0.0,
                    "content": clean_text,
                }
            continue

        if role == "assistant":
            finish_reason = str(r["finish_reason"] or "").strip().lower()
            has_tool_calls = bool(str(r["tool_calls"] or "").strip())
            if finish_reason == "tool_calls" or has_tool_calls or not clean_text:
                continue
            pending = pending_users.pop(sid, None)
            if pending is None:
                continue

            user_text = pending["content"]
            turn_kind = classify_user_turn(user_text)
            turn_text = f"User: {user_text}\nAssistant: {clean_text}".strip()
            stable_id = uid or str(row_id)
            event_id = f"hermes:{sid}:msg:{stable_id}"
            eff_ts = float(ts_val) if ts_val is not None else float(pending.get("ts") or 0.0)
            norm_hash = hashlib.sha256(_normalize_for_near_dedupe(turn_text).encode("utf-8")).hexdigest()
            # 只折叠「系统通知」的近重复：真人重复发言（如连发"继续"）必须各自成轮，
            # 否则会丢失真实轮次，也与既有语义冲突。
            if turn_kind == "system_notice" and _is_near_duplicate(last_by_session.get(sid), eff_ts, norm_hash):
                skipped_duplicate += 1
                continue

            occ_dt = (
                dt.datetime.fromtimestamp(eff_ts, tz=dt.timezone.utc)
                if eff_ts > 0
                else None
            )
            if turn_kind == "system_notice":
                event_kind = EVENT_KIND_REFERENCE_ONLY
                qualifies_nightly = False
            else:
                event_kind = EVENT_KIND_REFERENCE_ONLY if src == "cli" else EVENT_KIND_HUMAN_TURN
                qualifies_nightly = True

            res = store.ingest_event(
                client="hermes",
                session_id=sid,
                event_id=event_id,
                event_kind=event_kind,
                content=turn_text,
                occurred_at=occ_dt,
                meta={
                    "route": "state_db",
                    "source": src or "unknown",
                    "msg_id": row_id,
                    "message_uid": uid,
                    "turn_kind": turn_kind,
                },
                qualifies_nightly=qualifies_nightly,
                route="state_db",
            )
            if res.deduped:
                events_deduped += 1
            elif res.accepted:
                events_ingested += 1
                if event_kind == EVENT_KIND_HUMAN_TURN:
                    turns_completed += 1
            if eff_ts > 0:
                last_by_session[sid] = {"ts": eff_ts, "hash": norm_hash}

    # 限制未完成缓存体积，避免长期运行膨胀
    if len(pending_users) > 200:
        keep_keys = sorted(pending_users, key=lambda k: int(pending_users[k].get("id") or 0))[-200:]
        pending_users = {k: pending_users[k] for k in keep_keys}
    if len(last_by_session) > 200:
        keep_last = sorted(last_by_session, key=lambda k: float(last_by_session[k].get("ts") or 0.0))[-200:]
        last_by_session = {k: last_by_session[k] for k in keep_last}

    store.set_adapter_cursor(
        "hermes_statedb",
        db_str,
        {
            "last_msg_id": new_last_id,
            "pending_users": pending_users,
            "last_by_session": last_by_session,
        },
    )

    return HermesStateDbScanReport(
        db_path=db_str,
        available=True,
        sessions_scanned=len(sessions_seen),
        turns_completed=turns_completed,
        events_ingested=events_ingested,
        events_deduped=events_deduped,
        skipped_pre_activation=skipped_pre_activation,
        skipped_excluded=skipped_excluded,
        last_error=None,
        skipped_duplicate=skipped_duplicate,
    )
