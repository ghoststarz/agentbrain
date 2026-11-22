"""Stage 2 后台评审 Worker：上下文构建、预算分段、OpenAI 兼容 HTTP 模型调用与投影执行。"""

from __future__ import annotations

import inspect
import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentbrain.archive import apply_archive, plan_archive
from agentbrain.config import (
    AppConfig,
    ProactiveConfig,
    resolve_effective_model_backend,
    validate_proactive_worker_config,
)
from agentbrain.diary import collect_recent_diary_context
from agentbrain.index import touch_notes_access
from agentbrain.proactive import ClaimedJob, LeaseMismatchError, ProactiveStore, StoredEvent
from agentbrain.proactive_apply import (
    MAX_BATCH_OPS,
    ApplySummary,
    BatchValidationError,
    ConflictError,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import Clock, SystemClock, dt_to_us, ensure_utc
from agentbrain.proactive_hermes import HermesBridgeError, call_hermes_framework_model
from agentbrain.proactive_redaction import redact_secrets
from agentbrain.prompt_lib import PromptAssetError, load_prompt, load_soul
from agentbrain.retrieve import search_notes
from agentbrain.section_audit import run_section_audit

_EXCLUDED_CONTEXT_PREFIXES = ("30_Diary/", "30_Diary\\", "SOUL.md", "USER.md", "RULES.md")


def _system_prompt(
    job_type: str | None = None,
    *,
    window_id: str | None = None,
    soul_text: str | None = None,
    soul_path: Path | str | None = None,
    allow_diary: bool = True,
    idle_nightly: bool = False,
) -> str:
    """复盘主提示词：人格从 SOUL.md 运行时热读，与「怎么写」操作规则分离；夜间任务前置专用要求。

    `allow_diary=False` 用于夜间复盘的分段轮次：那几轮只产出 digest，日记留给最后一个分段统一写，
    避免多分段各写一份日记，把一天写成七八条重复流水账。
    `idle_nightly=True` 用于无新对话的空窗口夜间日记，使用 `review_nightly_idle`。
    """
    if soul_text is None:
        if soul_path is None:
            raise PromptAssetError("必须提供 soul_text 或 soul_path，禁止静默退化为空人格")
        resolved_soul = load_soul(soul_path)
    else:
        resolved_soul = str(soul_text).strip()
        if not resolved_soul:
            raise PromptAssetError("soul_text 不能为空，禁止静默退化为空人格")

    persona_block = f"## 你是谁（运行时读取自 SOUL.md，此处不得写死）\n\n{resolved_soul}"
    if job_type == "section_audit":
        audit_rules = load_prompt("section_audit")
        return f"{persona_block}\n\n{audit_rules}"
    base = load_prompt("review_system")
    if job_type == "nightly":
        if idle_nightly:
            addendum_name = "review_nightly_idle"
        elif allow_diary:
            addendum_name = "review_nightly_addendum"
        else:
            addendum_name = "review_nightly_segment"
        addendum = load_prompt(addendum_name)
        if window_id:
            addendum = addendum.replace("<窗口日期>", str(window_id))
        # 前置而不是追加：弱模型对末尾的补充要求容易忽略，实测前置才稳定遵守。
        return f"{addendum}\n\n{persona_block}\n\n{base}"
    if job_type == "nightly_work":
        addendum = load_prompt("review_nightly_work")
        if window_id:
            addendum = addendum.replace("<窗口日期>", str(window_id))
        return f"{addendum}\n\n{persona_block}\n\n{base}"
    if job_type == "nightly_diary":
        addendum = load_prompt("review_nightly_diary")
        if window_id:
            addendum = addendum.replace("<窗口日期>", str(window_id))
        return f"{addendum}\n\n{persona_block}\n\n{base}"
    return f"{persona_block}\n\n{base}"


def _nightly_system_prompt_overhead_chars(
    *,
    window_id: str | None,
    soul_text: str | None = None,
    soul_path: Path | str | None = None,
) -> int:
    """预算估算用：取「完整版」「分段版」「空窗口版」三种夜间提示词里最长的那个长度。"""
    lengths = []
    for allow, idle in ((True, False), (False, False), (True, True)):
        lengths.append(
            len(
                _system_prompt(
                    "nightly",
                    window_id=window_id,
                    soul_text=soul_text,
                    soul_path=soul_path,
                    allow_diary=allow,
                    idle_nightly=idle,
                )
            )
        )
    return max(lengths)


def _format_missing_diary_code(missing: list[str]) -> str:
    ordered = [m for m in ("public", "private", "wander") if m in set(missing)]
    if not ordered:
        return "nightly_missing_diary:both"
    if ordered == ["public", "private"]:
        return "nightly_missing_diary:both"
    if len(ordered) == 1:
        return f"nightly_missing_diary:{ordered[0]}"
    return "nightly_missing_diary:" + "+".join(ordered)


class NightlyMissingDiaryError(RuntimeError):
    """夜间复盘缺少必需的公账、私账或漫游（wander）日记条目。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        self.code = _format_missing_diary_code(self.missing)
        super().__init__(self.code)


def _extract_nightly_diary_ops_list(
    raw_output: Any,
    *,
    window_id: str | None,
) -> list[dict[str, Any]]:
    """从模型输出中提取所有符合夜间窗口日期的公/私账 diary_append 操作列表。"""
    if not isinstance(raw_output, dict):
        return []
    if str(raw_output.get("outcome") or "").strip() != "operations":
        return []
    ops = raw_output.get("operations")
    if not isinstance(ops, list):
        return []
    found: list[dict[str, Any]] = []
    target_date = str(window_id or "").strip()
    for op in ops:
        if not isinstance(op, dict):
            continue
        if str(op.get("op_type") or "").strip() != "diary_append":
            continue
        ledger = str(op.get("ledger") or "").strip().lower()
        if ledger not in ("public", "private"):
            continue
        if not str(op.get("content") or "").strip():
            continue
        if target_date and str(op.get("event_date") or "").strip() != target_date:
            continue
        found.append(op)
    return found


def _extract_nightly_diary_ops(
    raw_output: Any,
    *,
    window_id: str | None,
) -> dict[str, dict[str, Any]]:
    """兼容旧接口：按 ledger 返回首条符合夜间窗口日期的 diary_append。"""
    found: dict[str, dict[str, Any]] = {}
    for op in _extract_nightly_diary_ops_list(raw_output, window_id=window_id):
        ledger = str(op.get("ledger") or "").strip().lower()
        if ledger not in found:
            found[ledger] = op
    return found


def _count_nightly_diary_ops(ops: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"public": 0, "private": 0, "wander": 0}
    for op in ops:
        ledger = str(op.get("ledger") or "").strip().lower()
        kind = str(op.get("kind") or "daily").strip().lower() or "daily"
        if ledger == "public":
            counts["public"] += 1
        elif ledger == "private":
            counts["private"] += 1
            if kind == "wander":
                counts["wander"] += 1
    return counts


def _missing_nightly_diary_ledgers(
    raw_output: Any,
    *,
    window_id: str | None,
    satisfied: set[str] | None = None,
    satisfied_counts: dict[str, int] | None = None,
) -> list[str]:
    counts = _count_nightly_diary_ops(_extract_nightly_diary_ops_list(raw_output, window_id=window_id))
    if satisfied_counts:
        for k in ("public", "private", "wander"):
            counts[k] += int(satisfied_counts.get(k, 0))
    elif satisfied:
        if "public" in satisfied:
            counts["public"] += 1
        if "private" in satisfied:
            counts["private"] += 2
            counts["wander"] += 1
    missing: list[str] = []
    if counts["public"] < 1:
        missing.append("public")
    if counts["private"] < 2:
        missing.append("private")
    if counts["wander"] < 1:
        missing.append("wander")
    return missing


def _strip_diary_ops(raw_output: Any) -> tuple[Any, int]:
    """防御性剔除日记操作：多分段夜间前序分段与 nightly_work 不允许写日记。"""
    if not isinstance(raw_output, dict):
        return raw_output, 0
    ops = raw_output.get("operations")
    if not isinstance(ops, list):
        return raw_output, 0
    kept: list[Any] = []
    removed = 0
    for op in ops:
        if isinstance(op, dict) and str(op.get("op_type") or "").strip() == "diary_append":
            removed += 1
            continue
        kept.append(op)
    if removed == 0:
        return raw_output, 0
    cleaned = dict(raw_output)
    cleaned["operations"] = kept
    if not kept:
        cleaned["outcome"] = "no_change"
    return cleaned, removed


def _strip_non_diary_ops(raw_output: Any) -> tuple[Any, int]:
    """防御性剔除非日记操作：nightly_diary 通道只允许输出 diary_append。"""
    if not isinstance(raw_output, dict):
        return raw_output, 0
    ops = raw_output.get("operations")
    if not isinstance(ops, list):
        return raw_output, 0
    kept: list[Any] = []
    removed = 0
    for op in ops:
        if isinstance(op, dict) and str(op.get("op_type") or "").strip() == "diary_append":
            kept.append(op)
        else:
            removed += 1
    if removed == 0:
        return raw_output, 0
    cleaned = dict(raw_output)
    cleaned["operations"] = kept
    if not kept:
        cleaned["outcome"] = "no_change"
    return cleaned, removed


def _nightly_missing_retry_hint(missing: list[str], window_id: str | None) -> str:
    parts: list[str] = []
    if "public" in missing:
        parts.append("公账（ledger=public）至少 1 条")
    if "private" in missing:
        parts.append("私账（ledger=private）至少 2 条")
    if "wander" in missing:
        parts.append('私账中至少 1 条 kind="wander" 的漫游/小思考')
    missing_desc = "；".join(parts) if parts else "未满足夜间日记条数要求"
    date_part = f"（event_date 必须为 {window_id}）" if window_id else ""
    return (
        f"\n\nIMPORTANT: 本次为夜间复盘，未满足日记要求：{missing_desc}{date_part}。"
        "请补齐并输出合法 JSON（必须包含满足要求的 diary_append，且不要返回 no_change 或空操作），"
        "不要包含多余解释。"
    )


def _merge_nightly_diary_retry(
    first_out: dict[str, Any],
    retry_out: dict[str, Any],
    *,
    window_id: str | None,
) -> dict[str, Any]:
    """若重试仅补齐了缺失的部分日记，则保留首次输出中已合乎窗口日期的其余日记条目。"""
    if not isinstance(retry_out, dict):
        return retry_out
    if str(retry_out.get("outcome") or "").strip() != "operations":
        return retry_out
    retry_ops = retry_out.get("operations")
    if not isinstance(retry_ops, list) or not retry_ops:
        return retry_out
    # 若重试输出本身已完整满足公/私/wander 要求，直接采纳重试输出
    if not _missing_nightly_diary_ledgers(retry_out, window_id=window_id):
        return retry_out

    def _sig(op: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(op.get("ledger") or "").strip().lower(),
            str(op.get("kind") or "daily").strip().lower() or "daily",
            " ".join(str(op.get("content") or "").split()),
        )

    retry_sigs = {_sig(op) for op in _extract_nightly_diary_ops_list(retry_out, window_id=window_id)}
    first_diaries = _extract_nightly_diary_ops_list(first_out, window_id=window_id)
    prefix = [op for op in first_diaries if _sig(op) not in retry_sigs]
    if not prefix or len(prefix) + len(retry_ops) > MAX_BATCH_OPS:
        return retry_out
    merged = dict(retry_out)
    merged["operations"] = prefix + list(retry_ops)
    return merged


class ContextBudgetExceededError(RuntimeError):
    """单条事件超出模型上下文预算，拒绝静默丢弃证据。"""


class ModelTransportError(RuntimeError):
    """OpenAI 兼容 HTTP 模型调用或结构化 JSON 解析失败。"""


@dataclass(frozen=True)
class WorkerRunResult:
    """单次任务处理结果。"""

    job_id: int
    outcome: str
    segments_processed: int
    applied_count: int
    skipped_dedupe_count: int
    dry_run: bool
    model_route: dict[str, Any] | None = None


def _collect_existing_context(
    config: AppConfig,
    events: list[StoredEvent],
    budget_chars: int,
) -> tuple[str, dict[str, str]]:
    """检索相关项目与长期笔记（严格排除日记与身份文件），返回上下文文本与服务端读取快照哈希。"""
    snapshot_hashes: dict[str, str] = {}
    if budget_chars <= 200:
        return "", snapshot_hashes
    snippets: list[str] = []
    seen_paths: set[str] = set()
    used = 0

    # 0. 若 HOT.md 存在，纳入快照哈希与摘要
    hot_path = config.profile_dir / "10_Working" / "HOT.md"
    if hot_path.exists() and not hot_path.is_symlink():
        hot_text = hot_path.read_text(encoding="utf-8", errors="replace")
        hot_h = sha256_text(hot_text)
        snapshot_hashes["10_Working/HOT.md"] = hot_h
        hot_entry = f"[10_Working/HOT.md sha256={hot_h}]\n{redact_secrets(hot_text[:600])}"
        if used + len(hot_entry) + 2 <= budget_chars:
            snippets.append(hot_entry)
            seen_paths.add("10_Working/HOT.md")
            used += len(hot_entry) + 2

    # 1. 优先列出已有项目页摘要
    projects_dir = config.profile_dir / "20_LongTerm" / "Projects"
    if projects_dir.exists():
        for md in sorted(projects_dir.glob("*.md")):
            if md.name == "_Overview.md" or md.is_symlink():
                continue
            rel = f"20_LongTerm/Projects/{md.name}"
            text = md.read_text(encoding="utf-8", errors="replace")
            h = sha256_text(text)
            snapshot_hashes[rel] = h
            entry = f"[{rel} sha256={h}]\n{redact_secrets(text[:800])}"
            if used + len(entry) + 2 > budget_chars:
                break
            snippets.append(entry)
            seen_paths.add(rel)
            used += len(entry) + 2

    # 2. 按事件关键词检索相关长期笔记
    query_terms = " ".join(ev.content[:120] for ev in events[:5]).strip()
    if query_terms:
        try:
            res = search_notes(config, query_terms[:300], k_override=5, hops_override=1)
            for hit in res.get("hits") or []:
                rel = str(hit.get("path") or "").replace("\\", "/")
                if not rel or rel in seen_paths:
                    continue
                if any(rel.startswith(p) or rel.endswith(p) for p in _EXCLUDED_CONTEXT_PREFIXES):
                    continue
                full = config.profile_dir / rel
                if not full.exists() or full.is_symlink():
                    continue
                text = full.read_text(encoding="utf-8", errors="replace")
                h = sha256_text(text)
                snapshot_hashes[rel] = h
                entry = f"[{rel} sha256={h}]\n{redact_secrets(text[:600])}"
                if used + len(entry) + 2 > budget_chars:
                    break
                snippets.append(entry)
                seen_paths.add(rel)
                used += len(entry) + 2
        except Exception:
            pass

    if snapshot_hashes:
        touch_notes_access(config, snapshot_hashes.keys())

    return "\n\n".join(snippets), snapshot_hashes


def _format_event_block(
    ev: StoredEvent,
    *,
    notice_only: bool = False,
    max_content_chars: int | None = None,
) -> str:
    safe_content = redact_secrets(ev.content)
    if notice_only:
        safe_content = safe_content[:400]
    if max_content_chars is not None and max_content_chars > 0:
        safe_content = safe_content[:max_content_chars]
    meta_part = ""
    if ev.meta:
        safe_meta = {k: v for k, v in ev.meta.items() if k not in ("raw", "thinking")}
        if safe_meta:
            meta_part = f" meta={json.dumps(safe_meta, ensure_ascii=False)}"
    source_ref = f"{ev.client}:{ev.session_id}:{ev.event_id}"
    header = (
        f"[source_ref={source_ref} event_id={ev.event_id} kind={ev.event_kind} client={ev.client} "
        f"session={ev.session_id} occurred_at={ev.occurred_at_utc} turn_seq={ev.turn_seq}{meta_part}]"
    )
    if notice_only:
        return f"{header}\n[系统通知，仅参考]\n{safe_content}"
    return f"{header}\n{safe_content}"


def _build_recent_diary_block(config: AppConfig, *, window_id: str | None) -> str:
    """为夜间任务构建 `<recent_diary>` 上下文块（不含本窗口当天，受 days 与 max_chars 约束）。"""
    cfg = config.memory.proactive
    raw = collect_recent_diary_context(
        config,
        exclude_date=window_id,
        days=cfg.nightly_recent_diary_days,
        max_chars=cfg.nightly_recent_diary_max_chars,
    )
    if not raw:
        return ""
    safe = redact_secrets(raw)
    if len(safe) > cfg.nightly_recent_diary_max_chars:
        safe = safe[-cfg.nightly_recent_diary_max_chars :]
    return f"<recent_diary>\n{safe}\n</recent_diary>"


def estimate_nightly_context_chars(
    config: AppConfig,
    *,
    window_id: str | None,
    events: list[StoredEvent],
) -> int:
    """估算单任务夜间复盘总上下文长度（system + 证据 + existing_memory + recent_diary）。"""
    countable_events = sorted(
        (ev for ev in events if ev.countable),
        key=lambda e: (e.occurred_at_us, e.row_id),
    )
    uncountable_events = sorted(
        (ev for ev in events if not ev.countable),
        key=lambda e: (e.occurred_at_us, e.row_id),
    )[-2:]
    ordered_events = countable_events + uncountable_events

    max_ctx = config.memory.proactive.model_max_context_chars
    context_budget = min(max_ctx // 3, 6000)
    existing_ctx, _ = _collect_existing_context(config, ordered_events, context_budget)
    recent_diary_block = _build_recent_diary_block(config, window_id=window_id)
    sys_prompt_len = len(
        _system_prompt("nightly", window_id=window_id, soul_path=config.broker.soul_path)
    )
    evidence_len = sum(
        len(_format_event_block(ev, notice_only=not ev.countable)) + 2
        for ev in ordered_events
    )
    return sys_prompt_len + len(existing_ctx) + len(recent_diary_block) + evidence_len


def _split_oversized_block(event_id: str, block: str, event_budget: int) -> list[str]:
    """当单条事件 block 超出 event_budget 时，确定性按字符切为 N 段，不丢内容、不重复、顺序不变。"""
    if len(block) <= event_budget:
        return [block]

    total_parts = 2
    for _ in range(5):
        max_prefix = f"[part {total_parts}/{total_parts} of event_id={event_id}]\n"
        body_budget = event_budget - len(max_prefix)
        if body_budget <= 0:
            raise ContextBudgetExceededError(
                f"事件 {event_id} 的分段头长度 ({len(max_prefix)}) 超出单段预算 ({event_budget})"
            )
        needed = (len(block) + body_budget - 1) // body_budget
        if needed == total_parts:
            break
        total_parts = needed

    max_prefix = f"[part {total_parts}/{total_parts} of event_id={event_id}]\n"
    body_budget = event_budget - len(max_prefix)
    chunks = [block[i : i + body_budget] for i in range(0, len(block), body_budget)]
    actual_n = len(chunks)
    return [
        f"[part {idx}/{actual_n} of event_id={event_id}]\n{chunk}"
        for idx, chunk in enumerate(chunks, start=1)
    ]


def segment_job_prompts_with_snapshots(
    config: AppConfig,
    job: ClaimedJob,
    events: list[StoredEvent],
    *,
    store: ProactiveStore | None = None,
) -> tuple[list[tuple[list[StoredEvent], str]], dict[str, str]]:
    """按上下文预算确定性分段并返回服务端读取快照哈希；可计数事件优先，不可计数仅保留最近 2 条且截断到 400 字。"""
    countable_events = sorted(
        (ev for ev in events if ev.countable),
        key=lambda e: (e.occurred_at_us, e.row_id),
    )
    uncountable_events = sorted(
        (ev for ev in events if not ev.countable),
        key=lambda e: (e.occurred_at_us, e.row_id),
    )[-2:]
    ordered_events = countable_events + uncountable_events

    max_ctx = config.memory.proactive.model_max_context_chars
    header = (
        f"job_id={job.job_id} job_type={job.job_type} idempotency_key={job.idempotency_key} "
        f"window_id={job.window_id or ''} hot_max_chars={config.memory.hot_max_chars}\n"
    )
    context_budget = min(max_ctx // 3, 6000)
    existing_ctx, snapshot_hashes = _collect_existing_context(config, ordered_events, context_budget)
    recent_diary_block = ""
    if job.job_type in ("nightly", "nightly_diary"):
        recent_diary_block = _build_recent_diary_block(config, window_id=job.window_id)

    sys_prompt_len = len(
        _system_prompt(job.job_type, window_id=job.window_id, soul_path=config.broker.soul_path)
    )
    if job.job_type == "nightly":
        # 夜间可能是多分段：分段轮次用的是另一份补充要求，取两者更长的做预算，避免低估。
        sys_prompt_len = max(
            sys_prompt_len,
            _nightly_system_prompt_overhead_chars(
                window_id=job.window_id,
                soul_path=config.broker.soul_path,
            ),
        )
    recent_diary_section = f"\n\n{recent_diary_block}" if recent_diary_block else ""
    digest_section = ""
    if job.job_type == "nightly_diary" and store is not None and job.window_id and job.revision is not None:
        work_digests_cursor = store.get_adapter_cursor(
            "nightly_work_digests",
            f"{job.window_id}:rev:{job.revision}",
        )
        if work_digests_cursor and isinstance(work_digests_cursor.get("digests"), list):
            digests = [str(d).strip() for d in work_digests_cursor["digests"] if str(d).strip()]
            if digests:
                digest_section = (
                    "\n\n<segment_digests>\n"
                    + "\n".join(digests)
                    + "\n</segment_digests>"
                )

    static_overhead = (
        sys_prompt_len
        + len(header)
        + len(existing_ctx)
        + len(recent_diary_section)
        + len(digest_section)
        + 256
    )
    event_budget = max_ctx - static_overhead
    if event_budget < 256:
        raise ContextBudgetExceededError(
            f"上下文预算不足以容纳任何事件（max_ctx={max_ctx}, overhead={static_overhead}）"
        )

    def _assemble_prompt(ev_text: str, *, extra_tail: str = "") -> str:
        return (
            f"{header}\n<existing_memory>\n{existing_ctx}\n</existing_memory>"
            f"{recent_diary_section}{digest_section}\n\n"
            f"<untrusted_evidence>\n{ev_text}\n</untrusted_evidence>{extra_tail}"
        )

    if not ordered_events:
        if job.job_type in ("nightly", "nightly_diary"):
            prompt = _assemble_prompt(
                "",
                extra_tail=(
                    "\n\n说明：本窗口没有任何新对话，请只基于你自己的近况、想法与已有记忆写今天的公账与私账日记，"
                    "严禁编造用户当天做过什么。"
                ),
            )
        else:
            prompt = _assemble_prompt("")
        return [([], prompt)], snapshot_hashes

    # nightly_diary 通道：若当天原始证据超出预算，改喂「各分段 digest 汇总 + 每个事件的前 200 字」
    use_compact_diary_events = False
    if job.job_type == "nightly_diary":
        raw_total = sum(
            len(_format_event_block(ev, notice_only=not ev.countable)) + 2
            for ev in ordered_events
        )
        if raw_total > event_budget:
            use_compact_diary_events = True

    segments: list[tuple[list[StoredEvent], str]] = []
    cur_events: list[StoredEvent] = []
    cur_blocks: list[str] = []
    cur_len = 0

    for ev in ordered_events:
        raw_block = _format_event_block(
            ev,
            notice_only=not ev.countable,
            max_content_chars=200 if use_compact_diary_events else None,
        )
        parts = _split_oversized_block(ev.event_id, raw_block, event_budget)
        for part in parts:
            add_len = len(part) + (2 if cur_blocks else 0)
            if cur_blocks and (cur_len + add_len > event_budget):
                ev_text = "\n\n".join(cur_blocks)
                segments.append((cur_events, _assemble_prompt(ev_text)))
                cur_events = [ev]
                cur_blocks = [part]
                cur_len = len(part)
            else:
                if not cur_events or cur_events[-1].row_id != ev.row_id:
                    cur_events.append(ev)
                cur_blocks.append(part)
                cur_len += add_len

    if cur_blocks:
        ev_text = "\n\n".join(cur_blocks)
        segments.append((cur_events, _assemble_prompt(ev_text)))

    return segments, snapshot_hashes


def segment_job_prompts(
    config: AppConfig,
    job: ClaimedJob,
    events: list[StoredEvent],
) -> list[tuple[list[StoredEvent], str]]:
    """按上下文预算确定性分段；单条超预算显式失败，绝不静默丢弃未处理证据。"""
    segments, _ = segment_job_prompts_with_snapshots(config, job, events)
    return segments


def _resolve_chat_completions_url(endpoint: str) -> str:
    ep = endpoint.strip().rstrip("/")
    if ep.endswith("/chat/completions"):
        return ep
    if ep.endswith("/v1"):
        return f"{ep}/chat/completions"
    return f"{ep}/v1/chat/completions"


def call_openai_compatible_model(
    cfg: ProactiveConfig,
    user_prompt: str,
    *,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """通过标准库 urllib 调用 OpenAI 兼容 HTTP JSON 接口（受限超时，无工具调用）。"""
    validate_proactive_worker_config(cfg)
    url = _resolve_chat_completions_url(cfg.model_endpoint)
    api_key = os.environ.get(cfg.model_api_key_env, "").strip() if cfg.model_api_key_env else ""

    payload = {
        "model": cfg.model_name,
        "messages": [
            {"role": "system", "content": system_prompt if system_prompt is not None else _system_prompt()},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": cfg.model_max_output_tokens,
        "response_format": {"type": "json_object"},
    }
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    max_resp_bytes = max(65536, int(cfg.model_max_output_tokens) * 16)
    req = urllib.request.Request(url, data=raw_body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.model_timeout_seconds) as resp:
            raw_resp = resp.read(max_resp_bytes + 1)
            if len(raw_resp) > max_resp_bytes:
                raise ModelTransportError(f"模型响应体超出上限 ({len(raw_resp)} > {max_resp_bytes} bytes)")
            resp_text = raw_resp.decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ModelTransportError(f"模型 HTTP 错误 {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelTransportError(f"模型请求失败: {exc}") from exc

    try:
        parsed = json.loads(resp_text)
        choices = parsed["choices"]
        content = choices[0]["message"]["content"]
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            obj = json.loads(content)
            if not isinstance(obj, dict):
                raise ValueError("模型返回内容不是 JSON 对象")
            return obj
        raise ValueError("模型返回 message.content 类型无效")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelTransportError(f"模型响应结构化 JSON 解析失败: {exc}") from exc


def _supports_kwarg(fn: Callable[..., Any], kwarg: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return kwarg in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def call_proactive_model(
    config: AppConfig,
    user_prompt: str,
    *,
    hermes_runner: Callable[..., dict[str, Any]] | None = None,
    job_type: str | None = None,
    window_id: str | None = None,
    route_out: dict[str, Any] | None = None,
    allow_diary: bool = True,
    idle_nightly: bool = False,
) -> dict[str, Any]:
    """根据配置路由调用后台评审模型（默认 Hermes 框架路由，可选 OpenAI 兼容端点）。"""
    cfg = config.memory.proactive
    validate_proactive_worker_config(cfg)
    backend = resolve_effective_model_backend(cfg)
    sys_prompt = _system_prompt(
        job_type,
        window_id=window_id,
        soul_path=config.broker.soul_path,
        allow_diary=allow_diary,
        idle_nightly=idle_nightly,
    )
    if backend == "openai_compatible":
        if route_out is not None:
            route_out.clear()
            route_out.update(
                {
                    "route_source": "openai_compatible",
                    "route_resolved": True,
                    "resolved_provider": "openai_compatible",
                    "resolved_model": cfg.model_name,
                }
            )
        if _supports_kwarg(call_openai_compatible_model, "system_prompt"):
            return call_openai_compatible_model(cfg, user_prompt, system_prompt=sys_prompt)
        return call_openai_compatible_model(cfg, user_prompt)
    try:
        return call_hermes_framework_model(
            config,
            sys_prompt,
            user_prompt,
            runner=hermes_runner,
            route_out=route_out,
        )
    except HermesBridgeError as exc:
        raise ModelTransportError(str(exc)) from exc


def process_claimed_job(
    config: AppConfig,
    store: ProactiveStore,
    job: ClaimedJob,
    *,
    write_lock: threading.Lock | None = None,
    clock: Clock | None = None,
    dry_run: bool = False,
    lease_heartbeat: Callable[[], bool | None] | None = None,
    heartbeat_interval_seconds: float | None = None,
    hermes_runner: Callable[..., dict[str, Any]] | None = None,
) -> WorkerRunResult:
    """执行单个已认领任务的不可变分段快照、模型评审、租约续期与受限投影。"""
    if not config.memory.proactive.enabled:
        raise ModelTransportError("proactive.enabled 为 false，禁止执行后台模型评审")

    events = store.get_job_events(job.job_id)
    progress_key = f"job_segment_progress:{job.job_id}"
    cursor = store.get_adapter_cursor("worker_segment", progress_key)

    start_seg = 0
    total_applied = 0
    total_skipped = 0
    has_applied = False
    satisfied_counts: dict[str, int] = {"public": 0, "private": 0, "wander": 0}

    if (
        not dry_run
        and cursor is not None
        and isinstance(cursor.get("segments_prompts"), list)
        and isinstance(cursor.get("segment_event_keys"), list)
        and isinstance(cursor.get("snapshot_hashes"), dict)
        and len(cursor["segments_prompts"]) == len(cursor["segment_event_keys"])
    ):
        ev_by_key = {f"{e.client}:{e.session_id}:{e.event_id}": e for e in events}
        segments = [
            ([ev_by_key[k] for k in keys if k in ev_by_key], str(prompt))
            for keys, prompt in zip(cursor["segment_event_keys"], cursor["segments_prompts"])
        ]
        snapshot_hashes: dict[str, str] = {
            str(k): str(v) for k, v in cursor["snapshot_hashes"].items()
        }
        raw_initial = cursor.get("initial_snapshot_hashes")
        initial_snapshot_hashes: dict[str, str] = (
            {str(k): str(v) for k, v in raw_initial.items()}
            if isinstance(raw_initial, dict)
            else dict(snapshot_hashes)
        )
        raw_trusted = cursor.get("trusted_post_write_hashes")
        trusted_post_write_hashes: dict[str, str] = (
            {str(k): str(v) for k, v in raw_trusted.items()}
            if isinstance(raw_trusted, dict)
            else {}
        )
        start_seg = max(0, int(cursor.get("completed_segments") or 0))
        total_applied = max(0, int(cursor.get("total_applied") or 0))
        total_skipped = max(0, int(cursor.get("total_skipped") or 0))
        has_applied = bool(cursor.get("has_applied", total_applied > 0))
        raw_counts = cursor.get("nightly_diary_counts")
        if isinstance(raw_counts, dict):
            for k in ("public", "private", "wander"):
                satisfied_counts[k] = max(0, int(raw_counts.get(k) or 0))
        else:
            raw_ledgers = cursor.get("nightly_diary_ledgers")
            if isinstance(raw_ledgers, list):
                if "public" in raw_ledgers:
                    satisfied_counts["public"] = max(satisfied_counts["public"], 1)
                if "private" in raw_ledgers:
                    satisfied_counts["private"] = max(satisfied_counts["private"], 2)
                    satisfied_counts["wander"] = max(satisfied_counts["wander"], 1)
        raw_digests = cursor.get("earlier_digests")
        earlier_digests_state: list[str] = (
            [str(x) for x in raw_digests] if isinstance(raw_digests, list) else []
        )
        raw_written = cursor.get("written_notes")
        written_notes_state: list[str] = (
            [str(x) for x in raw_written] if isinstance(raw_written, list) else []
        )
    else:
        segments, snapshot_hashes = segment_job_prompts_with_snapshots(
            config,
            job,
            events,
            store=store,
        )
        initial_snapshot_hashes = dict(snapshot_hashes)
        trusted_post_write_hashes = {}
        earlier_digests_state = []
        written_notes_state = []
        if not dry_run:
            store.set_adapter_cursor(
                "worker_segment",
                progress_key,
                {
                    "completed_segments": 0,
                    "total_segments": len(segments),
                    "total_applied": 0,
                    "total_skipped": 0,
                    "has_applied": False,
                    "nightly_diary_counts": dict(satisfied_counts),
                    "earlier_digests": [],
                    "written_notes": [],
                    "snapshot_hashes": snapshot_hashes,
                    "initial_snapshot_hashes": initial_snapshot_hashes,
                    "trusted_post_write_hashes": trusted_post_write_hashes,
                    "segment_event_keys": [
                        [f"{e.client}:{e.session_id}:{e.event_id}" for e in evs]
                        for evs, _ in segments
                    ],
                    "segments_prompts": [p for _, p in segments],
                },
            )

    lease_sec = max(30, config.memory.proactive.lease_seconds)
    hb_interval = (
        max(0.001, float(heartbeat_interval_seconds))
        if heartbeat_interval_seconds is not None
        else max(1.0, min(30.0, lease_sec / 3.0))
    )
    hb_lock = threading.Lock()

    def _refresh_leases() -> None:
        if dry_run:
            return
        with hb_lock:
            store.renew_lease(job.job_id, job.lease_token, lease_seconds=lease_sec)
            if lease_heartbeat is not None:
                hb_res = lease_heartbeat()
                if hb_res is False:
                    raise LeaseMismatchError(f"任务 {job.job_id} runner 租约已失效，中止执行")

    _COMPACT_RETRY_HINT = "\n\n" + load_prompt("review_retry_compact")
    last_model_route: dict[str, Any] = {}
    is_idle_nightly = job.job_type == "nightly" and not events

    def _call_model_with_heartbeat(
        prompt: str,
        *,
        allow_compact_retry: bool = True,
        allow_diary: bool = True,
    ) -> dict[str, Any]:
        call_route: dict[str, Any] = {}
        if dry_run:
            out = call_proactive_model(
                config,
                prompt,
                hermes_runner=hermes_runner,
                job_type=job.job_type,
                window_id=job.window_id,
                route_out=call_route,
                allow_diary=allow_diary,
                idle_nightly=is_idle_nightly,
            )
            if call_route:
                last_model_route.clear()
                last_model_route.update(call_route)
            return out
        hb_stop = threading.Event()
        hb_error: list[BaseException] = []

        def _hb_loop() -> None:
            while not hb_stop.wait(hb_interval):
                try:
                    _refresh_leases()
                except BaseException as exc:  # noqa: BLE001
                    if not hb_error:
                        hb_error.append(exc)
                    hb_stop.set()
                    return

        hb_thread = threading.Thread(
            target=_hb_loop,
            name=f"proactive-hb-{job.job_id}",
            daemon=True,
        )
        hb_thread.start()
        try:
            try:
                out = call_proactive_model(
                    config,
                    prompt,
                    hermes_runner=hermes_runner,
                    job_type=job.job_type,
                    window_id=job.window_id,
                    route_out=call_route,
                    allow_diary=allow_diary,
                    idle_nightly=is_idle_nightly,
                )
                if call_route:
                    last_model_route.clear()
                    last_model_route.update(call_route)
                return out
            except ModelTransportError as exc:
                # 真实现场最常见的是输出被 max_tokens 截断导致 JSON 半截；重试一次并要求更紧凑的输出，
                # 而不是直接把整批证据判死（判死会静默丢掉这一批对话）。
                if allow_compact_retry and "JSON" in str(exc).upper():
                    return _call_model_with_heartbeat(
                        prompt + _COMPACT_RETRY_HINT,
                        allow_compact_retry=False,
                        allow_diary=allow_diary,
                    )
                raise
        finally:
            hb_stop.set()
            hb_thread.join(timeout=5.0)
            if hb_error:
                err = hb_error[0]
                if isinstance(err, LeaseMismatchError):
                    raise err
                raise LeaseMismatchError(f"任务 {job.job_id} 后台心跳续约失败: {err}") from err

    is_diary_nightly = job.job_type in ("nightly", "nightly_diary")
    is_work_nightly = job.job_type == "nightly_work"
    multi_nightly = job.job_type in ("nightly", "nightly_work") and len(segments) > 1
    earlier_digests: list[str] = list(earlier_digests_state)
    written_notes: list[str] = list(written_notes_state)

    for seg_idx, (seg_events, prompt) in enumerate(segments):
        if seg_idx < start_seg:
            continue
        _refresh_leases()
        is_last_seg = seg_idx == len(segments) - 1
        seg_prompt = prompt
        if multi_nightly and written_notes:
            seg_prompt = (
                seg_prompt
                + "\n\n<already_written_notes>\n"
                + "\n".join(written_notes[-20:])
                + "\n</already_written_notes>\n"
                + "（这些笔记本次复盘已经写过。同一条事实不要再新建文件：确有补充就沿用同一主题，"
                "宁可少写，也不要为了凑数再开一条。）"
            )
        if job.job_type == "nightly" and multi_nightly and is_last_seg and earlier_digests:
            seg_prompt = (
                seg_prompt
                + "\n\n<earlier_segment_digests>\n"
                + "\n".join(earlier_digests)
                + "\n</earlier_segment_digests>\n"
                + "（以上是前面几个分段已经整理过的当天轨迹摘要，写整天的公账/私账时一并考虑。）"
            )
        seg_allow_diary = (not is_work_nightly) and not (job.job_type == "nightly" and multi_nightly and not is_last_seg)
        model_out = _call_model_with_heartbeat(
            seg_prompt,
            allow_diary=seg_allow_diary,
        )
        _refresh_leases()
        if is_work_nightly or (job.job_type == "nightly" and multi_nightly and not is_last_seg):
            # nightly_work 与多分段前序分段不允许写日记：代码层强制剔除
            model_out, stripped = _strip_diary_ops(model_out)
            total_skipped += stripped
            digest = ""
            if isinstance(model_out, dict):
                digest = str(model_out.get("digest") or "").strip().replace("\n", " ")[:200]
            if digest:
                earlier_digests.append(f"- 分段 {seg_idx + 1}：{digest}")
        elif job.job_type == "nightly_diary":
            # nightly_diary 通道只做 diary_append：代码层滤掉其它 op
            model_out, stripped_non_diary = _strip_non_diary_ops(model_out)
            total_skipped += stripped_non_diary
        if is_diary_nightly and is_last_seg:
            missing = _missing_nightly_diary_ledgers(
                model_out,
                window_id=job.window_id,
                satisfied_counts=satisfied_counts,
            )
            if missing:
                retry_prompt = seg_prompt + _nightly_missing_retry_hint(missing, job.window_id)
                retry_out = _call_model_with_heartbeat(retry_prompt, allow_compact_retry=False)
                _refresh_leases()
                if job.job_type == "nightly_diary":
                    retry_out, stripped_retry = _strip_non_diary_ops(retry_out)
                    total_skipped += stripped_retry
                model_out = _merge_nightly_diary_retry(
                    model_out,
                    retry_out,
                    window_id=job.window_id,
                )
                missing_after = _missing_nightly_diary_ledgers(
                    model_out,
                    window_id=job.window_id,
                    satisfied_counts=satisfied_counts,
                )
                if missing_after:
                    raise NightlyMissingDiaryError(missing_after)
        def _apply_batch(payload: Any) -> ApplySummary:
            return validate_and_apply_batch(
                config,
                store,
                job,
                seg_events,
                payload,
                write_lock=write_lock,
                clock=clock or SystemClock(),
                dry_run=dry_run,
                snapshot_hashes=snapshot_hashes,
                initial_snapshot_hashes=initial_snapshot_hashes,
                trusted_post_write_hashes=trusted_post_write_hashes,
                lease_check=(lambda: store.has_valid_lease(job.job_id, job.lease_token)) if not dry_run else None,
            )

        try:
            summary: ApplySummary = _apply_batch(model_out)
        except BatchValidationError as exc:
            # 模型偶尔漏字段（最典型是某个操作没带 source_refs）。整批判死会白掉一整天证据，
            # 所以给一次「只修格式」的机会：把校验错误原文回灌，仍不合格才真失败。
            repair_hint = load_prompt("review_repair").replace("<ERROR>", f"{type(exc).__name__}: {exc}")
            repaired = _call_model_with_heartbeat(
                seg_prompt + "\n\n" + repair_hint,
                allow_compact_retry=False,
                allow_diary=seg_allow_diary,
            )
            _refresh_leases()
            if is_work_nightly or (job.job_type == "nightly" and multi_nightly and not is_last_seg):
                repaired, stripped2 = _strip_diary_ops(repaired)
                total_skipped += stripped2
            elif job.job_type == "nightly_diary":
                repaired, stripped2 = _strip_non_diary_ops(repaired)
                total_skipped += stripped2
            model_out = repaired
            summary = _apply_batch(model_out)
        if is_diary_nightly:
            seg_counts = _count_nightly_diary_ops(
                _extract_nightly_diary_ops_list(model_out, window_id=job.window_id)
            )
            for k in ("public", "private", "wander"):
                satisfied_counts[k] += seg_counts[k]
        if summary.post_write_hashes:
            snapshot_hashes.update(summary.post_write_hashes)
            trusted_post_write_hashes.update(summary.post_write_hashes)
        for rel in summary.post_write_hashes or {}:
            clean_rel = str(rel).strip()
            if clean_rel.startswith("20_LongTerm/") and clean_rel not in written_notes:
                written_notes.append(clean_rel)
        total_applied += summary.applied_count
        total_skipped += summary.skipped_dedupe_count
        if summary.outcome == "applied" or summary.applied_count > 0:
            has_applied = True
        if not dry_run:
            store.set_adapter_cursor(
                "worker_segment",
                progress_key,
                {
                    "completed_segments": seg_idx + 1,
                    "total_segments": len(segments),
                    "total_applied": total_applied,
                    "total_skipped": total_skipped,
                    "has_applied": has_applied,
                    "nightly_diary_counts": dict(satisfied_counts),
                    "earlier_digests": list(earlier_digests),
                    "written_notes": list(written_notes),
                    "snapshot_hashes": snapshot_hashes,
                    "initial_snapshot_hashes": initial_snapshot_hashes,
                    "trusted_post_write_hashes": trusted_post_write_hashes,
                    "segment_event_keys": [
                        [f"{e.client}:{e.session_id}:{e.event_id}" for e in evs]
                        for evs, _ in segments
                    ],
                    "segments_prompts": [p for _, p in segments],
                },
            )

    if is_work_nightly and earlier_digests and not dry_run and job.window_id and job.revision is not None:
        store.set_adapter_cursor(
            "nightly_work_digests",
            f"{job.window_id}:rev:{job.revision}",
            {"digests": list(earlier_digests)},
        )

    if is_diary_nightly:
        missing_final: list[str] = []
        if satisfied_counts["public"] < 1:
            missing_final.append("public")
        if satisfied_counts["private"] < 2:
            missing_final.append("private")
        if satisfied_counts["wander"] < 1:
            missing_final.append("wander")
        if missing_final:
            raise NightlyMissingDiaryError(missing_final)
        if not dry_run and job.window_id:
            missing_disk: list[str] = []
            pub_path = config.profile_dir / "30_Diary" / "Public" / f"{job.window_id}.md"
            pri_path = config.profile_dir / "30_Diary" / "Private" / f"{job.window_id}.md"
            pub_lines = (
                [ln for ln in pub_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip().startswith("- [")]
                if pub_path.exists()
                else []
            )
            pri_lines = (
                [ln for ln in pri_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip().startswith("- [")]
                if pri_path.exists()
                else []
            )
            if len(pub_lines) < 1:
                missing_disk.append("public")
            if len(pri_lines) < 2:
                missing_disk.append("private")
            if not any("[wander]" in ln for ln in pri_lines):
                missing_disk.append("wander")
            if missing_disk:
                raise NightlyMissingDiaryError(missing_disk)

    return WorkerRunResult(
        job_id=job.job_id,
        outcome="applied" if has_applied else "no_change",
        segments_processed=max(0, len(segments) - start_seg),
        applied_count=total_applied,
        skipped_dedupe_count=total_skipped,
        dry_run=dry_run,
        model_route=dict(last_model_route) if last_model_route else None,
    )


def run_worker_once(
    config: AppConfig,
    store: ProactiveStore,
    *,
    worker_id: str = "worker-1",
    write_lock: threading.Lock | None = None,
    clock: Clock | None = None,
    dry_run: bool = False,
    lease_heartbeat: Callable[[], bool | None] | None = None,
    heartbeat_interval_seconds: float | None = None,
    hermes_runner: Callable[..., dict[str, Any]] | None = None,
) -> WorkerRunResult | None:
    """认领并处理一个可执行任务；dry-run 不修改队列/租约/游标；失败时精确 ack_job_failure。"""
    if not config.memory.proactive.enabled:
        return None
    if not dry_run:
        store.reconcile_countable_events()
    job = store.peek_next_claimable_job() if dry_run else store.claim_job(worker_id)
    if job is None:
        return None
    try:
        res = process_claimed_job(
            config,
            store,
            job,
            write_lock=write_lock,
            clock=clock,
            dry_run=dry_run,
            lease_heartbeat=lease_heartbeat,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            hermes_runner=hermes_runner,
        )
        if not dry_run:
            summary_payload: dict[str, Any] = {
                "segments": res.segments_processed,
                "applied": res.applied_count,
                "skipped_dedupe": res.skipped_dedupe_count,
            }
            if res.model_route:
                summary_payload["model_route"] = res.model_route
            store.ack_job_success(
                job.job_id,
                job.lease_token,
                outcome=res.outcome,
                summary=summary_payload,
            )
            # 先写复盘结果，再依次跑夜间小节体检与归档；异常绝不把已成功的复盘改成失败。
            if job.job_type in ("nightly", "nightly_work") and config.memory.proactive.section_audit_enabled:
                try:
                    run_section_audit(
                        config,
                        job_id=job.job_id,
                        clock=clock or SystemClock(),
                        write_lock=write_lock,
                        hermes_runner=hermes_runner,
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    try:
                        store.record_section_audit_failed_receipt(
                            job_id=job.job_id,
                            error=f"{type(audit_exc).__name__}: {audit_exc}",
                        )
                    except Exception:
                        pass
            if job.job_type in ("nightly", "nightly_work") and config.memory.proactive.archive_enabled:
                try:
                    eff_clock = clock or SystemClock()
                    now_utc = ensure_utc(eff_clock.now_utc())
                    now_us = dt_to_us(now_utc)
                    plan = plan_archive(
                        config,
                        now_us,
                        days=config.memory.proactive.archive_after_days,
                    )
                    if plan:
                        apply_archive(config, plan, store=store, now_utc=now_utc)
                except Exception as arch_exc:  # noqa: BLE001
                    try:
                        store.record_archive_failed_receipt(
                            job_id=job.job_id,
                            error=f"{type(arch_exc).__name__}: {arch_exc}",
                        )
                    except Exception:
                        pass
        return res
    except (
        BatchValidationError,
        ConflictError,
        ContextBudgetExceededError,
        LeaseMismatchError,
        ModelTransportError,
        NightlyMissingDiaryError,
        Exception,
    ) as exc:
        if not dry_run:
            try:
                is_missing_diary = isinstance(exc, NightlyMissingDiaryError)
                store.ack_job_failure(
                    job.job_id,
                    job.lease_token,
                    error=exc.code if is_missing_diary else f"{type(exc).__name__}: {exc}",
                    force_dead=is_missing_diary,
                )
            except LeaseMismatchError:
                pass
        return None
