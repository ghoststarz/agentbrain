"""Stage 2 数据源适配器：agy IDE transcript JSONL 增量扫描与 agy CLI 引用事件采集。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbrain.proactive import (
    EVENT_KIND_HUMAN_TURN,
    EVENT_KIND_REFERENCE_ONLY,
    ProactiveStore,
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
) -> tuple[int, int]:
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
        return 0, 0

    if cursor is None:
        # 激活后新建的对话文件或显式 catchup：从头处理
        byte_offset = 0
        last_step = -1
        generation = 0
        pending_user = None
    else:
        byte_offset = int(cursor.get("byte_offset", 0))
        last_step = int(cursor.get("last_step_index", -1))
        generation = int(cursor.get("generation", 0))
        pending_user = cursor.get("pending_user_turn")
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
                    turn_text = f"User: {pending_user['content'].strip()}\nAssistant: {content.strip()}"
                    gen_suffix = f":g{generation}" if generation > 0 else ""
                    res = store.ingest_event(
                        client="agy_ide",
                        session_id=f"{root_tag}:{conv_id}",
                        event_id=f"agy_ide:{root_tag}:{conv_id}{gen_suffix}:turn:{u_step}-{step_index}",
                        event_kind=EVENT_KIND_HUMAN_TURN,
                        content=turn_text,
                        occurred_at=occ_dt,
                        meta={"user_step": u_step, "model_step": step_index, "conv_id": conv_id},
                        qualifies_nightly=True,
                    )
                    if res.accepted and not res.deduped:
                        ingested += 1
                        turns += 1
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
        },
    )
    return ingested, turns


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
            ing, trn = _scan_single_transcript(
                store,
                conv_dir.name,
                jsonl_path,
                catchup=catchup,
                is_initial_baseline=is_initial_baseline,
                root_tag=root_tag,
            )
            events_ingested += ing
            turns_completed += trn

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
