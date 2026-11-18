"""Stage 2 后台评审 Worker：上下文构建、预算分段、OpenAI 兼容 HTTP 模型调用与投影执行。"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentbrain.config import (
    AppConfig,
    ProactiveConfig,
    resolve_effective_model_backend,
    validate_proactive_worker_config,
)
from agentbrain.proactive import ClaimedJob, LeaseMismatchError, ProactiveStore, StoredEvent
from agentbrain.proactive_apply import (
    MAX_BATCH_OPS,
    ApplySummary,
    BatchValidationError,
    ConflictError,
    sha256_text,
    validate_and_apply_batch,
)
from agentbrain.proactive_clock import Clock, SystemClock
from agentbrain.proactive_hermes import HermesBridgeError, call_hermes_framework_model
from agentbrain.proactive_redaction import redact_secrets
from agentbrain.retrieve import search_notes

_EXCLUDED_CONTEXT_PREFIXES = ("30_Diary/", "30_Diary\\", "SOUL.md", "USER.md", "RULES.md")

from agentbrain.prompt_lib import load_prompt


def _system_prompt(job_type: str | None = None, *, window_id: str | None = None) -> str:
    """复盘主提示词：外部 md 文件，改了即时生效；夜间任务把专用要求**前置**。"""
    base = load_prompt("review_system")
    if job_type == "nightly":
        addendum = load_prompt("review_nightly_addendum")
        if window_id:
            addendum = addendum.replace("<窗口日期>", str(window_id))
        # 前置而不是追加：弱模型对末尾的补充要求容易忽略，实测前置才稳定遵守。
        return f"{addendum}\n\n{base}"
    return base


def _format_missing_diary_code(missing: list[str]) -> str:
    missing_set = set(missing)
    if "public" in missing_set and "private" in missing_set:
        return "nightly_missing_diary:both"
    if "public" in missing_set:
        return "nightly_missing_diary:public"
    if "private" in missing_set:
        return "nightly_missing_diary:private"
    return "nightly_missing_diary:both"


class NightlyMissingDiaryError(RuntimeError):
    """夜间复盘缺少必需的公账或私账日记条目。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        self.code = _format_missing_diary_code(self.missing)
        super().__init__(self.code)


def _extract_nightly_diary_ops(
    raw_output: Any,
    *,
    window_id: str | None,
) -> dict[str, dict[str, Any]]:
    """从模型输出中提取符合夜间窗口日期的公/私账 diary_append 操作（按 ledger 索引）。"""
    if not isinstance(raw_output, dict):
        return {}
    if str(raw_output.get("outcome") or "").strip() != "operations":
        return {}
    ops = raw_output.get("operations")
    if not isinstance(ops, list):
        return {}
    found: dict[str, dict[str, Any]] = {}
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
        if ledger not in found:
            found[ledger] = op
    return found


def _missing_nightly_diary_ledgers(
    raw_output: Any,
    *,
    window_id: str | None,
    satisfied: set[str] | None = None,
) -> list[str]:
    found = set(_extract_nightly_diary_ops(raw_output, window_id=window_id).keys())
    if satisfied:
        found |= satisfied
    return [ledger for ledger in ("public", "private") if ledger not in found]


def _nightly_missing_retry_hint(missing: list[str], window_id: str | None) -> str:
    if set(missing) == {"public", "private"}:
        missing_desc = "缺少 public 和 private 日记"
    elif "public" in missing:
        missing_desc = "缺少 public 日记"
    else:
        missing_desc = "缺少 private 日记"
    date_part = f"（event_date 必须为 {window_id}）" if window_id else ""
    return (
        f"\n\nIMPORTANT: 本次为夜间复盘，{missing_desc}{date_part}。"
        "请补齐并输出合法 JSON（必须包含 diary_append，且不要返回 no_change 或空操作），"
        "不要包含多余解释。"
    )


def _merge_nightly_diary_retry(
    first_out: dict[str, Any],
    retry_out: dict[str, Any],
    *,
    window_id: str | None,
) -> dict[str, Any]:
    """若重试仅补齐了缺失的一侧日记，则保留首次输出中已合乎窗口日期的另一侧日记。"""
    if not isinstance(retry_out, dict):
        return retry_out
    if str(retry_out.get("outcome") or "").strip() != "operations":
        return retry_out
    retry_ops = retry_out.get("operations")
    if not isinstance(retry_ops, list) or not retry_ops:
        return retry_out
    retry_diaries = _extract_nightly_diary_ops(retry_out, window_id=window_id)
    first_diaries = _extract_nightly_diary_ops(first_out, window_id=window_id)
    prefix = [
        op
        for ledger in ("public", "private")
        if ledger not in retry_diaries and ledger in first_diaries
        for op in (first_diaries[ledger],)
    ]
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

    return "\n\n".join(snippets), snapshot_hashes


def _format_event_block(ev: StoredEvent, *, notice_only: bool = False) -> str:
    safe_content = redact_secrets(ev.content)
    if notice_only:
        safe_content = safe_content[:400]
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


def segment_job_prompts_with_snapshots(
    config: AppConfig,
    job: ClaimedJob,
    events: list[StoredEvent],
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
    static_overhead = len(_system_prompt(job.job_type, window_id=job.window_id)) + len(header) + len(existing_ctx) + 256
    event_budget = max_ctx - static_overhead
    if event_budget < 256:
        raise ContextBudgetExceededError(
            f"上下文预算不足以容纳任何事件（max_ctx={max_ctx}, overhead={static_overhead}）"
        )

    if not ordered_events:
        prompt = (
            f"{header}\n<existing_memory>\n{existing_ctx}\n</existing_memory>\n\n"
            "<untrusted_evidence>\n</untrusted_evidence>"
        )
        return [([], prompt)], snapshot_hashes

    segments: list[tuple[list[StoredEvent], str]] = []
    cur_events: list[StoredEvent] = []
    cur_blocks: list[str] = []
    cur_len = 0

    for ev in ordered_events:
        block = _format_event_block(ev, notice_only=not ev.countable)
        if len(block) > event_budget:
            raise ContextBudgetExceededError(
                f"单条事件 {ev.event_id} 长度 ({len(block)}) 超出单段预算 ({event_budget})，拒绝静默丢弃"
            )
        add_len = len(block) + (2 if cur_blocks else 0)
        if cur_blocks and (cur_len + add_len > event_budget):
            ev_text = "\n\n".join(cur_blocks)
            prompt = (
                f"{header}\n<existing_memory>\n{existing_ctx}\n</existing_memory>\n\n"
                f"<untrusted_evidence>\n{ev_text}\n</untrusted_evidence>"
            )
            segments.append((cur_events, prompt))
            cur_events = [ev]
            cur_blocks = [block]
            cur_len = len(block)
        else:
            cur_events.append(ev)
            cur_blocks.append(block)
            cur_len += add_len

    if cur_blocks:
        ev_text = "\n\n".join(cur_blocks)
        prompt = (
            f"{header}\n<existing_memory>\n{existing_ctx}\n</existing_memory>\n\n"
            f"<untrusted_evidence>\n{ev_text}\n</untrusted_evidence>"
        )
        segments.append((cur_events, prompt))

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


def call_proactive_model(
    config: AppConfig,
    user_prompt: str,
    *,
    hermes_runner: Callable[..., dict[str, Any]] | None = None,
    job_type: str | None = None,
    window_id: str | None = None,
) -> dict[str, Any]:
    """根据配置路由调用后台评审模型（默认 Hermes 框架路由，可选 OpenAI 兼容端点）。"""
    cfg = config.memory.proactive
    validate_proactive_worker_config(cfg)
    backend = resolve_effective_model_backend(cfg)
    sys_prompt = _system_prompt(job_type, window_id=window_id)
    if backend == "openai_compatible":
        if job_type == "nightly":
            return call_openai_compatible_model(cfg, user_prompt, system_prompt=sys_prompt)
        return call_openai_compatible_model(cfg, user_prompt)
    try:
        return call_hermes_framework_model(
            config,
            sys_prompt,
            user_prompt,
            runner=hermes_runner,
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
    satisfied_ledgers: set[str] = set()

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
        raw_ledgers = cursor.get("nightly_diary_ledgers")
        if isinstance(raw_ledgers, list):
            satisfied_ledgers = {
                str(x).strip().lower()
                for x in raw_ledgers
                if str(x).strip().lower() in ("public", "private")
            }
    else:
        segments, snapshot_hashes = segment_job_prompts_with_snapshots(config, job, events)
        initial_snapshot_hashes = dict(snapshot_hashes)
        trusted_post_write_hashes = {}
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
                    "nightly_diary_ledgers": [],
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

    def _call_model_with_heartbeat(prompt: str, *, allow_compact_retry: bool = True) -> dict[str, Any]:
        if dry_run:
            return call_proactive_model(
                config,
                prompt,
                hermes_runner=hermes_runner,
                job_type=job.job_type,
            )
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
                return call_proactive_model(
                    config,
                    prompt,
                    hermes_runner=hermes_runner,
                    job_type=job.job_type,
                    window_id=job.window_id,
                )
            except ModelTransportError as exc:
                # 真实现场最常见的是输出被 max_tokens 截断导致 JSON 半截；重试一次并要求更紧凑的输出，
                # 而不是直接把整批证据判死（判死会静默丢掉这一批对话）。
                if allow_compact_retry and "JSON" in str(exc).upper():
                    return _call_model_with_heartbeat(
                        prompt + _COMPACT_RETRY_HINT,
                        allow_compact_retry=False,
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

    for seg_idx, (seg_events, prompt) in enumerate(segments):
        if seg_idx < start_seg:
            continue
        _refresh_leases()
        model_out = _call_model_with_heartbeat(prompt)
        _refresh_leases()
        is_last_seg = seg_idx == len(segments) - 1
        if job.job_type == "nightly" and is_last_seg:
            missing = _missing_nightly_diary_ledgers(
                model_out,
                window_id=job.window_id,
                satisfied=satisfied_ledgers,
            )
            if missing:
                retry_prompt = prompt + _nightly_missing_retry_hint(missing, job.window_id)
                retry_out = _call_model_with_heartbeat(retry_prompt, allow_compact_retry=False)
                _refresh_leases()
                model_out = _merge_nightly_diary_retry(
                    model_out,
                    retry_out,
                    window_id=job.window_id,
                )
                missing_after = _missing_nightly_diary_ledgers(
                    model_out,
                    window_id=job.window_id,
                    satisfied=satisfied_ledgers,
                )
                if missing_after:
                    raise NightlyMissingDiaryError(missing_after)
        summary: ApplySummary = validate_and_apply_batch(
            config,
            store,
            job,
            seg_events,
            model_out,
            write_lock=write_lock,
            clock=clock or SystemClock(),
            dry_run=dry_run,
            snapshot_hashes=snapshot_hashes,
            initial_snapshot_hashes=initial_snapshot_hashes,
            trusted_post_write_hashes=trusted_post_write_hashes,
            lease_check=(lambda: store.has_valid_lease(job.job_id, job.lease_token)) if not dry_run else None,
        )
        if job.job_type == "nightly":
            satisfied_ledgers |= set(
                _extract_nightly_diary_ops(model_out, window_id=job.window_id).keys()
            )
        if summary.post_write_hashes:
            snapshot_hashes.update(summary.post_write_hashes)
            trusted_post_write_hashes.update(summary.post_write_hashes)
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
                    "nightly_diary_ledgers": sorted(satisfied_ledgers),
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

    if job.job_type == "nightly":
        missing_final = [l for l in ("public", "private") if l not in satisfied_ledgers]
        if missing_final:
            raise NightlyMissingDiaryError(missing_final)
        if not dry_run and job.window_id:
            missing_disk: list[str] = []
            if not (config.profile_dir / "30_Diary" / "Public" / f"{job.window_id}.md").exists():
                missing_disk.append("public")
            if not (config.profile_dir / "30_Diary" / "Private" / f"{job.window_id}.md").exists():
                missing_disk.append("private")
            if missing_disk:
                raise NightlyMissingDiaryError(missing_disk)

    return WorkerRunResult(
        job_id=job.job_id,
        outcome="applied" if has_applied else "no_change",
        segments_processed=max(0, len(segments) - start_seg),
        applied_count=total_applied,
        skipped_dedupe_count=total_skipped,
        dry_run=dry_run,
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
            store.ack_job_success(
                job.job_id,
                job.lease_token,
                outcome=res.outcome,
                summary={
                    "segments": res.segments_processed,
                    "applied": res.applied_count,
                    "skipped_dedupe": res.skipped_dedupe_count,
                },
            )
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
