"""Stage 2 受限输出校验、冲突检测、幂等去重与 Vault 投影模块。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from agentbrain.acl import display_client_id
from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig
from agentbrain.diary import DiaryGateError, append_diary
from agentbrain.handoff import handoff_path, read_handoff, upsert_handoff
from agentbrain.index import reindex
from agentbrain.proactive import ClaimedJob, LeaseMismatchError, ProactiveStore, StoredEvent
from agentbrain.proactive_clock import Clock, SystemClock, ensure_utc

MANAGED_START = "<!-- agentbrain:managed:start -->"
MANAGED_END = "<!-- agentbrain:managed:end -->"

ALLOWED_OP_TYPES = frozenset({
    "longterm_upsert",
    "project_update",
    "hot_update",
    "handoff_upsert",
    "diary_append",
})

_LONGTERM_DIRS: dict[str, str] = {
    "fact": "20_LongTerm/Facts",
    "decision": "20_LongTerm/Decisions",
    "entity": "20_LongTerm/Entities",
    "moc": "20_LongTerm/MOC",
}

_FORBIDDEN_NAMES = frozenset({
    "soul.md",
    "user.md",
    "rules.md",
    "gemini.md",
    "memory.yaml",
    "broker.yaml",
    "tokens.yaml",
})

_FORBIDDEN_DIRS = ("30_Diary", "90_Archive", ".git", ".obsidian")
_SAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_EVIDENCE_LEVELS = frozenset({"report", "delivered", "verified"})
_DIARY_LINE_RE = re.compile(r"^-\s*\[\d{2}:\d{2}:\d{2}\](?:\s*\([^)]*\))?\s+(.*)$")
_PROJECT_EV_LINE_RE = re.compile(r"(?im)^\s*(?:evidence_level|verification_status)\s*:\s*.*$")

MAX_BATCH_OPS = 25
MAX_LONGTERM_CHARS = 16000
MAX_PROJECT_MANAGED_CHARS = 8000
MAX_HANDOFF_SUMMARY_CHARS = 2000
MAX_DIARY_CHARS = 4000


class BatchValidationError(ValueError):
    """模型输出批次校验失败（拒绝整批，零写入）。"""


class ConflictError(RuntimeError):
    """目标文件哈希与 expected_hash / 读取快照不一致，拒绝覆盖手改内容。"""


@dataclass(frozen=True)
class ApplySummary:
    """一次受限输出投影的结果摘要。"""

    outcome: str
    applied_count: int
    skipped_dedupe_count: int
    reindexed: bool
    operations: list[dict[str, Any]]
    post_write_hashes: dict[str, str] = field(default_factory=dict)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sanitize_filename(name: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("-", str(name or "").strip()).strip().strip(".")
    return cleaned[:80] if cleaned else "note"


def _assert_no_symlink_escape(config: AppConfig, rel_path: str) -> Path:
    """校验相对路径无目录穿越与符号链接逃逸（适用于所有受管写入路径含日记与 handoff）。"""
    raw = str(rel_path or "").strip()
    if not raw or Path(raw).is_absolute() or ":" in raw:
        raise BatchValidationError(f"非法相对路径: {rel_path!r}")
    parts = Path(raw).parts
    if any(p in ("..", ".", "") for p in parts):
        raise BatchValidationError(f"路径包含非法片段: {rel_path!r}")

    profile_root = config.profile_dir.resolve()
    if config.profile_dir.exists() and config.profile_dir.is_symlink():
        raise BatchValidationError("禁止通过符号链接 profile 目录写入")

    cur = config.profile_dir
    for part in parts:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise BatchValidationError(f"禁止通过符号链接写入: {rel_path!r}")

    candidate = config.profile_dir / Path(*parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(profile_root):
        raise BatchValidationError(f"目标路径逃逸出 profile 目录: {rel_path!r}")
    return candidate


def _assert_safe_profile_subpath(config: AppConfig, rel_path: str) -> Path:
    """严格校验普通受限写入路径：禁止身份/配置/归档/日记直写与符号链接逃逸。"""
    candidate = _assert_no_symlink_escape(config, rel_path)
    parts = Path(str(rel_path).strip()).parts
    lower_name = parts[-1].lower()
    if lower_name in _FORBIDDEN_NAMES:
        raise BatchValidationError(f"禁止写入受保护文件: {rel_path!r}")
    if any(parts[0].lower() == d.lower() for d in _FORBIDDEN_DIRS):
        raise BatchValidationError(f"禁止直接写入受限目录: {rel_path!r}")
    return candidate


def _normalize_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _canonical_event_ref(ev: StoredEvent) -> str:
    return f"{ev.client}:{ev.session_id}:{ev.event_id}"


def _resolve_source_refs(
    raw_refs: Any,
    events: list[StoredEvent],
    op_idx: int,
    op_type: str,
) -> tuple[list[str], list[str], list[StoredEvent]]:
    """强制校验 source_refs 非空且全部真实存在于当前事件批次中。"""
    refs = _normalize_str_list(raw_refs)
    if not refs:
        raise BatchValidationError(f"operation[{op_idx}] {op_type} 必须提供非空 source_refs")

    by_canonical: dict[str, StoredEvent] = {}
    by_event_id: dict[str, list[StoredEvent]] = {}
    for ev in events:
        by_canonical[_canonical_event_ref(ev)] = ev
        by_event_id.setdefault(ev.event_id, []).append(ev)

    matched: list[StoredEvent] = []
    canonical_out: list[str] = []
    seen_canonical: set[str] = set()
    for ref in refs:
        ev = by_canonical.get(ref)
        if ev is None:
            candidates = by_event_id.get(ref, [])
            if len(candidates) > 1:
                raise BatchValidationError(
                    f"operation[{op_idx}] source_refs 包含跨会话歧义引用 {ref!r}，请使用 client:session_id:event_id"
                )
            if len(candidates) == 1:
                ev = candidates[0]
        if ev is None:
            raise BatchValidationError(
                f"operation[{op_idx}] source_refs 引用了不存在于当前批次的事件: {ref!r}"
            )
        c_ref = _canonical_event_ref(ev)
        if c_ref not in seen_canonical:
            seen_canonical.add(c_ref)
            canonical_out.append(c_ref)
            matched.append(ev)

    return refs, sorted(canonical_out), matched


def _has_source_validation_evidence(matched_events: list[StoredEvent]) -> bool:
    """仅当被引用源事件中存在真实工具/测试验证证据时才允许 verified 级别。"""
    for ev in matched_events:
        if ev.meta.get("verified") is True or ev.meta.get("verification_passed") is True:
            return True
        if ev.event_kind == "reference_only" and ev.meta.get("tool_status") in ("passed", "verified", "ok"):
            return True
    return False


def _resolve_evidence_level(
    raw_level: Any,
    matched_events: list[StoredEvent],
    op_idx: int,
) -> str:
    ev_level = str(raw_level or "report").strip().lower()
    if ev_level not in _EVIDENCE_LEVELS:
        raise BatchValidationError(f"operation[{op_idx}] 非法 evidence_level: {ev_level!r}")
    if ev_level == "verified" and not _has_source_validation_evidence(matched_events):
        return "report"
    return ev_level


def _trusted_source_and_domain(
    config: AppConfig,
    matched_events: list[StoredEvent],
) -> tuple[str, str]:
    """从可信源事件身份推导对外展示 source (hermes/antigravity) 与 domain，绝不由模型自填。"""
    raw_client = matched_events[0].client if matched_events else "proactive"
    clean = str(raw_client or "").strip()
    lower = clean.lower()
    if lower == "hermes" or lower.startswith("hermes:"):
        display_source = "hermes"
    elif lower in ("antigravity", "agy_ide", "agy_cli") or lower.startswith("agy"):
        display_source = "antigravity"
    else:
        display_source = display_client_id(clean) or "proactive"

    domain = "work"
    for key in (clean, display_source):
        cfg = config.memory.clients.get(key)
        if isinstance(cfg, dict) and cfg.get("domain"):
            domain = str(cfg["domain"])
            break
    return display_source, domain


def _parse_frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    """解析 Markdown YAML frontmatter 与正文。"""
    if text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.splitlines(keepends=True)
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is not None:
            fm_raw = "".join(lines[1:end_idx])
            body = "".join(lines[end_idx + 1 :])
            try:
                parsed = yaml.safe_load(fm_raw)
                if isinstance(parsed, dict):
                    return dict(parsed), body
            except yaml.YAMLError:
                pass
    return {}, text


def _dump_frontmatter(fm: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---\n"


def _extract_managed_block(text: str) -> str | None:
    s_idx = text.find(MANAGED_START)
    e_idx = text.find(MANAGED_END)
    if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
        return text[s_idx + len(MANAGED_START) : e_idx].strip()
    return None


def _merge_managed_block(
    existing_text: str | None,
    *,
    default_header: str,
    managed_body: str,
) -> str:
    """仅替换受管块内文本，完整保留受管块之外的所有手工内容与 frontmatter。"""
    block = f"{MANAGED_START}\n{managed_body.strip()}\n{MANAGED_END}"
    if existing_text is None:
        return f"{default_header}{block}\n"

    start_idx = existing_text.find(MANAGED_START)
    end_idx = existing_text.find(MANAGED_END)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        before = existing_text[:start_idx]
        after = existing_text[end_idx + len(MANAGED_END) :]
        return f"{before}{block}{after}"

    sep = "\n\n" if existing_text and not existing_text.endswith("\n\n") else ""
    if existing_text and not existing_text.endswith("\n"):
        sep = "\n\n"
    return f"{existing_text}{sep}{block}\n"


def _find_existing_fact_key_path(config: AppConfig, fact_key: str) -> str | None:
    """按 fact_key 在 20_LongTerm 下查找已有槽位笔记，避免仅按标题新建重复文件。"""
    lt_dir = config.profile_dir / "20_LongTerm"
    if not lt_dir.exists() or lt_dir.is_symlink():
        return None
    for md in sorted(lt_dir.rglob("*.md")):
        if md.name == "_Overview.md" or md.is_symlink():
            continue
        try:
            rel = md.relative_to(config.profile_dir).as_posix()
            _assert_safe_profile_subpath(config, rel)
        except Exception:
            continue
        fm, _ = _parse_frontmatter_and_body(md.read_text(encoding="utf-8", errors="replace"))
        if str(fm.get("fact_key") or "").strip() == fact_key:
            return rel
    return None


def _format_longterm_note_content(
    *,
    existing_text: str | None,
    note_type: str,
    title: str,
    content: str,
    keys: list[str],
    tags: list[str],
    fact_key: str | None,
    evidence_level: str,
    source_refs: list[str],
    trusted_source: str,
    trusted_domain: str,
    today_str: str,
) -> str:
    """生成或更新长期笔记：新笔记含完整 parse contract 键，已有笔记保留原 frontmatter 与手工正文。"""
    if existing_text is None:
        fm: dict[str, Any] = {
            "type": note_type,
            "title": title,
            "proactive_owned": True,
            "created": today_str,
            "updated": today_str,
            "source": trusted_source,
            "domain": trusted_domain,
            "status": "active",
            "evidence_level": evidence_level,
        }
        if fact_key:
            fm["fact_key"] = fact_key
        if keys:
            fm["keys"] = keys
        if tags:
            fm["tags"] = tags
        if source_refs:
            fm["source_refs"] = source_refs
        header = f"{_dump_frontmatter(fm)}\n# {title}\n\n"
        return _merge_managed_block(None, default_header=header, managed_body=content)

    fm, body = _parse_frontmatter_and_body(existing_text)
    if not fm.get("type"):
        fm["type"] = note_type
    if not fm.get("title"):
        fm["title"] = title
    if not fm.get("created"):
        fm["created"] = today_str
    fm["updated"] = today_str
    if not fm.get("source"):
        fm["source"] = trusted_source
    if not fm.get("domain"):
        fm["domain"] = trusted_domain
    if not fm.get("status"):
        fm["status"] = "active"
    fm["evidence_level"] = evidence_level
    if fact_key:
        fm["fact_key"] = fact_key
    if keys:
        fm["keys"] = keys
    if tags:
        fm["tags"] = tags
    if source_refs:
        fm["source_refs"] = source_refs

    merged_body = _merge_managed_block(
        body.lstrip("\r\n"),
        default_header=f"# {title}\n\n",
        managed_body=content,
    )
    return f"{_dump_frontmatter(fm)}\n{merged_body}"


def _format_project_managed_body(managed_content: str, evidence_level: str) -> str:
    """由服务端生成结构化 evidence_level 头，防止 managed_content 绕过校验伪造 verified。"""
    cleaned_lines = [
        ln for ln in managed_content.splitlines() if not _PROJECT_EV_LINE_RE.match(ln)
    ]
    body = "\n".join(cleaned_lines).strip()
    if evidence_level != "verified":
        body = re.sub(r"(?im)\bstatus\s*:\s*verified\b", "status: reported", body)
    return f"evidence_level: {evidence_level}\n{body}".strip()


def _regenerate_project_overview(config: AppConfig) -> Path | None:
    """仅更新 _Overview.md 的受管块，完整保留其手工段落与前后文本，并校验路径/符号链接。"""
    overview_rel = "20_LongTerm/Projects/_Overview.md"
    overview_path = _assert_safe_profile_subpath(config, overview_rel)
    projects_dir = overview_path.parent
    if not projects_dir.exists():
        return None
    if projects_dir.is_symlink():
        raise BatchValidationError("禁止通过符号链接 Projects 目录生成 _Overview.md")

    entries: list[tuple[str, str]] = []
    for md_file in sorted(projects_dir.glob("*.md")):
        if md_file.name == "_Overview.md" or md_file.is_symlink():
            continue
        text = md_file.read_text(encoding="utf-8", errors="replace")
        managed = _extract_managed_block(text)
        if managed:
            entries.append((md_file.stem, managed))

    if not entries:
        return None

    managed_lines: list[str] = []
    for name, managed in entries:
        managed_lines.append(f"## [[{name}]]")
        managed_lines.append("")
        managed_lines.append(managed)
        managed_lines.append("")
    managed_body = "\n".join(managed_lines).strip()

    existing_text = overview_path.read_text(encoding="utf-8") if overview_path.exists() else None
    default_header = (
        "---\n"
        "type: moc\n"
        'title: "Projects Overview"\n'
        "generated_by: proactive_stage2\n"
        "---\n\n"
        "# Projects Overview\n\n"
    )
    merged = _merge_managed_block(
        existing_text,
        default_header=default_header,
        managed_body=managed_body,
    )
    atomic_write_text(overview_path, merged)
    return overview_path


def _resolve_diary_datetime(
    op: dict[str, Any],
    job: ClaimedJob,
    matched_events: list[StoredEvent],
    clock: Clock,
) -> dt.datetime:
    """日记日期严格限定在源事件本地日期或夜间窗口日期范围内，绝不接受模型任意日期。"""
    allowed_dates: set[str] = set()
    for ev in matched_events:
        utc_dt = ensure_utc(dt.datetime.fromisoformat(ev.occurred_at_utc))
        allowed_dates.add(clock.to_local(utc_dt).strftime("%Y-%m-%d"))
    if job.job_type == "nightly" and job.window_id:
        allowed_dates.add(job.window_id)

    raw_date = str(op.get("event_date") or "").strip()
    if raw_date:
        try:
            parsed_d = dt.date.fromisoformat(raw_date)
        except ValueError as exc:
            raise BatchValidationError(f"diary_append 非法 event_date: {raw_date!r}") from exc
        if allowed_dates and parsed_d.isoformat() not in allowed_dates:
            raise BatchValidationError(
                f"diary_append event_date {raw_date!r} 超出源事件/窗口日期范围 {sorted(allowed_dates)}"
            )
        return dt.datetime(parsed_d.year, parsed_d.month, parsed_d.day, 22, 0, 0)

    if matched_events:
        utc_dt = ensure_utc(dt.datetime.fromisoformat(matched_events[0].occurred_at_utc))
        return clock.to_local(utc_dt).replace(tzinfo=None)

    if job.window_id:
        win_date = dt.date.fromisoformat(job.window_id)
        return dt.datetime(win_date.year, win_date.month, win_date.day, 22, 55, 0)

    return clock.to_local(clock.now_utc()).replace(tzinfo=None)


def _diary_contains_exact_entry(config: AppConfig, ledger: str, date_str: str, content: str) -> bool:
    """精确行正文比对（绝不使用子串匹配），用于崩溃恢复幂等。"""
    sub = "Public" if ledger == "public" else "Private"
    target = config.profile_dir / "30_Diary" / sub / f"{date_str}.md"
    if not target.exists():
        return False
    norm_new = " ".join(content.split())
    if not norm_new:
        return False
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DIARY_LINE_RE.match(line.strip())
        if not m:
            continue
        if " ".join(m.group(1).split()) == norm_new:
            return True
    return False


def _replace_diary_auto_entry(
    target: Path,
    old_content: str,
    new_content: str,
    *,
    now_dt: dt.datetime,
) -> bool:
    """在日记文件中就地更新同一证据来源的自动条目（精确匹配旧正文，绝不误改其他条目）。"""
    if not target.exists():
        return False
    norm_old = " ".join(old_content.split())
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    for idx, line in enumerate(lines):
        m = _DIARY_LINE_RE.match(line.strip())
        if m and " ".join(m.group(1).split()) == norm_old:
            time_str = now_dt.strftime("%H:%M:%S")
            lines[idx] = f"- [{time_str}] (proactive) {new_content.strip()}\n"
            atomic_write_text(target, "".join(lines))
            return True
    return False


def _derive_op_id(job: ClaimedJob, idx: int, op_type: str, dedupe_key: str) -> str:
    digest = sha256_text(f"{op_type}:{dedupe_key}")[:12]
    return f"{job.idempotency_key}:op:{idx}:{digest}"


def _derive_dedupe_key(op: dict[str, Any]) -> str:
    op_type = str(op["op_type"])
    if op_type == "diary_append":
        ledger = str(op.get("ledger") or "").strip().lower()
        date_hint = str(op.get("_resolved_date") or "")
        canonical_refs = op.get("_canonical_source_refs") or []
        refs_digest = sha256_text("|".join(canonical_refs))[:24]
        return f"diary:{ledger}:{date_hint}:refs:{refs_digest}"
    if op_type == "longterm_upsert":
        fact_key = str(op.get("fact_key") or "").strip()
        slot = fact_key or str(op.get("_resolved_rel") or "")
        content_hash = sha256_text(" ".join(str(op.get("content") or "").split()))[:16]
        return f"slot:{slot}:{content_hash}"
    if op_type == "project_update":
        project = _sanitize_filename(str(op.get("project") or op.get("title") or ""))
        block_hash = sha256_text(str(op.get("managed_content") or "").strip())[:16]
        return f"project:{project}:{block_hash}"
    if op_type == "hot_update":
        return f"hot:{sha256_text(str(op.get('content') or '').strip())[:16]}"
    if op_type == "handoff_upsert":
        tk = str(op.get("task_key") or "").strip()
        st = str(op.get("status") or "").strip().lower()
        sm = sha256_text(str(op.get("summary") or "").strip())[:16]
        return f"handoff:{tk}:{st}:{sm}"
    return sha256_text(json.dumps(op, sort_keys=True, ensure_ascii=False))[:24]


def _is_proactive_owned_note(path: Path) -> bool:
    """判断目标是否为主动复盘生成的受管笔记（而非用户手写笔记）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    head = parts[1] if len(parts) >= 2 else ""
    if "proactive_owned: true" in head:
        return True
    # 旧版本生成的长期笔记：frontmatter 同时带 evidence_level 与 source_refs。
    return "evidence_level:" in head and "source_refs:" in head


def _check_hash_conflict(
    *,
    rel: str,
    target: Path,
    expected_hash: str,
    snapshot_hashes: dict[str, str] | None,
    initial_snapshot_hashes: dict[str, str] | None = None,
    trusted_post_write_hashes: dict[str, str] | None = None,
) -> str | None:
    """校验新旧目标文件的预期哈希（缺失目标带 expected_hash 视为创建冲突，已有目标必须有匹配哈希）。"""
    snap_hash = (snapshot_hashes or {}).get(rel, "")
    trusted_hash = (trusted_post_write_hashes or {}).get(rel, "")
    initial_hash = (initial_snapshot_hashes or {}).get(rel, "")
    if not target.exists():
        if expected_hash:
            raise ConflictError(f"创建冲突：目标 {rel} 不存在但提供了 expected_hash={expected_hash[:12]}")
        if snap_hash or trusted_hash:
            raise ConflictError(f"冲突：读取快照中存在 {rel}，但当前文件已消失")
        return None

    if trusted_hash and expected_hash and initial_hash and expected_hash == initial_hash:
        # 同一任务前序分段已写入该目标，后续分段若沿用任务初快照哈希，改用可信写后哈希校验外部并发修改
        req_hash = trusted_hash
    else:
        req_hash = expected_hash or trusted_hash or snap_hash
    if not req_hash:
        # 系统自己生成的笔记（frontmatter 带 evidence_level/source_refs）跨任务续写时，
        # 模型这一批的上下文里未必包含该文件（快照只收预算内的相关笔记），因此拿不到哈希。
        # 只要目标确实是本系统生成的受管笔记、且用户没有手工改写其受管块之外的内容，
        # 允许按受管块合并更新；人工笔记仍然必须提供哈希，不能靠这条放松覆盖手写内容。
        if _is_proactive_owned_note(target):
            return None
        raise ConflictError(f"更新已有文件 {rel} 必须提供 expected_hash 或服务端读取快照哈希")
    cur_hash = sha256_text(target.read_text(encoding="utf-8"))
    if cur_hash != req_hash:
        raise ConflictError(f"冲突拒绝覆盖 {rel}: expected={req_hash[:12]} actual={cur_hash[:12]}")
    return req_hash


def validate_and_apply_batch(
    config: AppConfig,
    store: ProactiveStore,
    job: ClaimedJob,
    events: list[StoredEvent],
    raw_output: dict[str, Any],
    *,
    write_lock: threading.Lock | None = None,
    clock: Clock | None = None,
    dry_run: bool = False,
    snapshot_hashes: dict[str, str] | None = None,
    initial_snapshot_hashes: dict[str, str] | None = None,
    trusted_post_write_hashes: dict[str, str] | None = None,
    lease_check: Callable[[], bool] | None = None,
) -> ApplySummary:
    """先整批校验，再在 broker 写锁下复检哈希与租约并幂等执行投影与重索引。"""
    if not isinstance(raw_output, dict):
        raise BatchValidationError("模型输出必须为 JSON 对象")
    outcome = str(raw_output.get("outcome") or "").strip()
    if outcome not in ("no_change", "operations"):
        raise BatchValidationError(f"非法 outcome: {outcome!r}")
    ops = raw_output.get("operations")
    if outcome == "no_change":
        if ops not in (None, []):
            raise BatchValidationError("outcome=no_change 时 operations 必须为空")
        return ApplySummary(
            outcome="no_change",
            applied_count=0,
            skipped_dedupe_count=0,
            reindexed=False,
            operations=[],
        )

    if not isinstance(ops, list):
        raise BatchValidationError("operations 必须为列表")
    if len(ops) > MAX_BATCH_OPS:
        raise BatchValidationError(f"operations 数量超限 ({len(ops)} > {MAX_BATCH_OPS})")
    if len(ops) == 0:
        return ApplySummary(
            outcome="no_change",
            applied_count=0,
            skipped_dedupe_count=0,
            reindexed=False,
            operations=[],
        )

    eff_clock = clock or SystemClock()
    today_str = eff_clock.to_local(eff_clock.now_utc()).strftime("%Y-%m-%d")
    validated_ops: list[dict[str, Any]] = []
    pre_lock_checks: list[tuple[Path, str, str | None]] = []

    # 第一阶段：整批严格校验（任意一条不合规即中止，零写入）
    for idx, raw_op in enumerate(ops):
        if not isinstance(raw_op, dict):
            raise BatchValidationError(f"operation[{idx}] 必须为对象")
        if raw_op.get("delete") is True or raw_op.get("action") == "delete":
            raise BatchValidationError(f"operation[{idx}] 禁止删除操作")
        op_type = str(raw_op.get("op_type") or "").strip()
        if op_type not in ALLOWED_OP_TYPES:
            raise BatchValidationError(f"operation[{idx}] 不支持的 op_type: {op_type!r}")

        op = dict(raw_op)
        source_refs, canonical_refs, matched_events = _resolve_source_refs(
            op.get("source_refs"),
            events,
            idx,
            op_type,
        )
        op["source_refs"] = source_refs
        op["_canonical_source_refs"] = canonical_refs
        op["_matched_events"] = matched_events

        if "target_path" in op and op["target_path"]:
            _assert_safe_profile_subpath(config, str(op["target_path"]))

        if op_type == "longterm_upsert":
            note_type = str(op.get("note_type") or "").strip().lower()
            if note_type not in _LONGTERM_DIRS:
                raise BatchValidationError(f"operation[{idx}] 非法 note_type: {note_type!r}")
            title = str(op.get("title") or "").strip()
            content = str(op.get("content") or "").strip()
            if not title or not content:
                raise BatchValidationError(f"operation[{idx}] longterm_upsert 缺少 title 或 content")
            if len(content) > MAX_LONGTERM_CHARS:
                raise BatchValidationError(
                    f"operation[{idx}] longterm_upsert 超出字符上限 ({len(content)} > {MAX_LONGTERM_CHARS})"
                )
            fact_key = str(op.get("fact_key") or "").strip() or None
            existing_rel = _find_existing_fact_key_path(config, fact_key) if fact_key else None
            rel = existing_rel or f"{_LONGTERM_DIRS[note_type]}/{_sanitize_filename(title)}.md"
            target = _assert_safe_profile_subpath(config, rel)
            expected_hash = str(op.get("expected_hash") or "").strip()
            req_hash = _check_hash_conflict(
                rel=rel,
                target=target,
                expected_hash=expected_hash,
                snapshot_hashes=snapshot_hashes,
                initial_snapshot_hashes=initial_snapshot_hashes,
                trusted_post_write_hashes=trusted_post_write_hashes,
            )
            pre_lock_checks.append((target, rel, req_hash))
            op["evidence_level"] = _resolve_evidence_level(op.get("evidence_level"), matched_events, idx)
            trusted_source, trusted_domain = _trusted_source_and_domain(config, matched_events)
            op["_trusted_source"] = trusted_source
            op["_trusted_domain"] = trusted_domain
            op["_resolved_rel"] = rel
            op["_resolved_target"] = target

        elif op_type == "project_update":
            project = str(op.get("project") or op.get("title") or "").strip()
            raw_managed = str(op.get("managed_content") or op.get("content") or "").strip()
            if not project or not raw_managed:
                raise BatchValidationError(f"operation[{idx}] project_update 缺少 project 或 managed_content")
            if len(raw_managed) > MAX_PROJECT_MANAGED_CHARS:
                raise BatchValidationError(
                    f"operation[{idx}] project_update 超出字符上限 ({len(raw_managed)} > {MAX_PROJECT_MANAGED_CHARS})"
                )
            slug = _sanitize_filename(project)
            rel = f"20_LongTerm/Projects/{slug}.md"
            target = _assert_safe_profile_subpath(config, rel)
            expected_hash = str(op.get("expected_hash") or "").strip()
            req_hash = _check_hash_conflict(
                rel=rel,
                target=target,
                expected_hash=expected_hash,
                snapshot_hashes=snapshot_hashes,
                initial_snapshot_hashes=initial_snapshot_hashes,
                trusted_post_write_hashes=trusted_post_write_hashes,
            )
            pre_lock_checks.append((target, rel, req_hash))
            ev_level = _resolve_evidence_level(op.get("evidence_level"), matched_events, idx)
            op["project"] = slug
            op["evidence_level"] = ev_level
            op["managed_content"] = _format_project_managed_body(raw_managed, ev_level)
            op["_resolved_rel"] = rel
            op["_resolved_target"] = target

        elif op_type == "hot_update":
            content = str(op.get("content") or "").strip()
            if not content:
                raise BatchValidationError(f"operation[{idx}] hot_update 缺少 content")
            if len(content) > config.memory.hot_max_chars:
                raise BatchValidationError(
                    f"operation[{idx}] hot_update 超出字符上限 ({len(content)} > {config.memory.hot_max_chars})"
                )
            rel = "10_Working/HOT.md"
            target = _assert_safe_profile_subpath(config, rel)
            expected_hash = str(op.get("expected_hash") or "").strip()
            req_hash = _check_hash_conflict(
                rel=rel,
                target=target,
                expected_hash=expected_hash,
                snapshot_hashes=snapshot_hashes,
                initial_snapshot_hashes=initial_snapshot_hashes,
                trusted_post_write_hashes=trusted_post_write_hashes,
            )
            pre_lock_checks.append((target, rel, req_hash))
            op["_resolved_rel"] = rel
            op["_resolved_target"] = target

        elif op_type == "handoff_upsert":
            task_key = str(op.get("task_key") or "").strip()
            status = str(op.get("status") or "open").strip()
            summary = str(op.get("summary") or "").strip()
            if not task_key or not summary:
                raise BatchValidationError(f"operation[{idx}] handoff_upsert 缺少 task_key 或 summary")
            if len(summary) > MAX_HANDOFF_SUMMARY_CHARS:
                raise BatchValidationError(
                    f"operation[{idx}] handoff_upsert summary 超出字符上限 ({len(summary)} > {MAX_HANDOFF_SUMMARY_CHARS})"
                )
            rel = "10_Working/.state/handoff.json"
            _assert_no_symlink_escape(config, rel)
            op["task_key"] = task_key
            op["status"] = status
            op["summary"] = summary
            op["_resolved_rel"] = rel

        elif op_type == "diary_append":
            ledger = str(op.get("ledger") or "").strip().lower()
            content = str(op.get("content") or "").strip()
            if ledger not in ("public", "private"):
                raise BatchValidationError(f"operation[{idx}] 非法 diary ledger: {ledger!r}")
            if not content:
                raise BatchValidationError(f"operation[{idx}] diary_append 缺少 content")
            if len(content) > MAX_DIARY_CHARS:
                raise BatchValidationError(
                    f"operation[{idx}] diary_append 超出字符上限 ({len(content)} > {MAX_DIARY_CHARS})"
                )
            if ledger == "private" and config.memory.diary_gate:
                raise DiaryGateError("private diary gate is enabled")
            target_dt = _resolve_diary_datetime(op, job, matched_events, eff_clock)
            date_str = target_dt.strftime("%Y-%m-%d")
            rel = f"30_Diary/{'Public' if ledger == 'public' else 'Private'}/{date_str}.md"
            _assert_no_symlink_escape(config, rel)
            op["ledger"] = ledger
            op["_resolved_dt"] = target_dt
            op["_resolved_date"] = date_str
            op["_resolved_rel"] = rel

        dedupe_key = _derive_dedupe_key(op)
        op_id = str(op.get("op_id") or "").strip() or _derive_op_id(job, idx, op_type, dedupe_key)
        op["_op_id"] = op_id
        op["_dedupe_key"] = dedupe_key
        validated_ops.append(op)

    if dry_run:
        return ApplySummary(
            outcome="operations",
            applied_count=0,
            skipped_dedupe_count=0,
            reindexed=False,
            operations=[
                {
                    "op_id": op["_op_id"],
                    "op_type": op["op_type"],
                    "target_rel": op["_resolved_rel"],
                    "dedupe_key": op["_dedupe_key"],
                    "dry_run": True,
                }
                for op in validated_ops
            ],
        )

    lock = write_lock or threading.Lock()
    applied_count = 0
    skipped_dedupe = 0
    touched_non_diary = False
    touched_project = False
    op_records: list[dict[str, Any]] = []
    post_write_hashes: dict[str, str] = {}

    with lock:
        # 写入前在 broker 写锁下复检租约权威性与所有目标文件哈希
        if lease_check is not None and not lease_check():
            raise LeaseMismatchError(f"任务 {job.job_id} 租约已失效，中止写入")

        seen_targets_in_batch: set[str] = set()
        for target_p, rel, req_hash in pre_lock_checks:
            if rel in seen_targets_in_batch:
                continue
            seen_targets_in_batch.add(rel)
            if req_hash is None:
                if target_p.exists() and not _is_proactive_owned_note(target_p):
                    raise ConflictError(f"写锁复检冲突：目标 {rel} 在校验后被外部创建")
            else:
                if not target_p.exists():
                    raise ConflictError(f"写锁复检冲突：目标 {rel} 在校验后已消失")
                actual_hash = sha256_text(target_p.read_text(encoding="utf-8"))
                if actual_hash != req_hash:
                    raise ConflictError(
                        f"写锁复检冲突拒绝覆盖 {rel}: expected={req_hash[:12]} actual={actual_hash[:12]}"
                    )

        for op in validated_ops:
            op_id = op["_op_id"]
            op_type = op["op_type"]
            dedupe_key = op["_dedupe_key"]
            rel = op["_resolved_rel"]

            # 仅按 op_id 检查当前任务重放幂等；状态类不作全局历史内容去重，避免抑制 A->B->A
            if store.is_op_applied(op_id):
                skipped_dedupe += 1
                op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_dedupe"})
                continue

            if op_type == "diary_append":
                journal = store.get_adapter_cursor("diary_journal", dedupe_key)
                norm_new = " ".join(op["content"].split())
                if journal is not None:
                    prev_content = str(journal.get("content") or "")
                    if " ".join(prev_content.split()) == norm_new:
                        store.record_applied_op(
                            op_id=op_id,
                            job_id=job.job_id,
                            job_idempotency_key=job.idempotency_key,
                            op_type=op_type,
                            dedupe_key=dedupe_key,
                            target_path=rel,
                            target_date=op["_resolved_date"],
                            source_refs=op["source_refs"],
                        )
                        skipped_dedupe += 1
                        op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_dedupe"})
                        continue
                    diary_target = _assert_no_symlink_escape(config, rel)
                    if _replace_diary_auto_entry(
                        diary_target,
                        prev_content,
                        op["content"],
                        now_dt=op["_resolved_dt"],
                    ):
                        store.set_adapter_cursor(
                            "diary_journal",
                            dedupe_key,
                            {
                                "target_path": rel,
                                "target_date": op["_resolved_date"],
                                "content": op["content"],
                                "op_id": op_id,
                            },
                        )
                        store.record_applied_op(
                            op_id=op_id,
                            job_id=job.job_id,
                            job_idempotency_key=job.idempotency_key,
                            op_type=op_type,
                            dedupe_key=dedupe_key,
                            target_path=rel,
                            target_date=op["_resolved_date"],
                            source_refs=op["source_refs"],
                        )
                        applied_count += 1
                        op_records.append(
                            {"op_id": op_id, "op_type": op_type, "status": "updated", "target_rel": rel}
                        )
                        continue

                if _diary_contains_exact_entry(config, op["ledger"], op["_resolved_date"], op["content"]):
                    store.set_adapter_cursor(
                        "diary_journal",
                        dedupe_key,
                        {
                            "target_path": rel,
                            "target_date": op["_resolved_date"],
                            "content": op["content"],
                            "op_id": op_id,
                        },
                    )
                    store.record_applied_op(
                        op_id=op_id,
                        job_id=job.job_id,
                        job_idempotency_key=job.idempotency_key,
                        op_type=op_type,
                        dedupe_key=dedupe_key,
                        target_path=rel,
                        target_date=op["_resolved_date"],
                        source_refs=op["source_refs"],
                    )
                    skipped_dedupe += 1
                    op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_crash_idempotent"})
                    continue

                append_diary(
                    config,
                    ledger=op["ledger"],
                    content=op["content"],
                    author=("antigravity" if str(job.client or "").startswith(("agy", "antigravity")) else "hermes"),
                    can_casual=False,
                    now=op["_resolved_dt"],
                )
                store.set_adapter_cursor(
                    "diary_journal",
                    dedupe_key,
                    {
                        "target_path": rel,
                        "target_date": op["_resolved_date"],
                        "content": op["content"],
                        "op_id": op_id,
                    },
                )

            elif op_type == "longterm_upsert":
                target: Path = op["_resolved_target"]
                existing = target.read_text(encoding="utf-8") if target.exists() else None
                if existing is not None:
                    cur_managed = _extract_managed_block(existing)
                    if cur_managed is not None and cur_managed == str(op["content"]).strip():
                        fm_cur, _ = _parse_frontmatter_and_body(existing)
                        if (
                            str(fm_cur.get("evidence_level") or "") == op["evidence_level"]
                            and str(fm_cur.get("fact_key") or "") == (str(op.get("fact_key") or "").strip())
                        ):
                            skipped_dedupe += 1
                            op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                            continue
                target.parent.mkdir(parents=True, exist_ok=True)
                rendered = _format_longterm_note_content(
                    existing_text=existing,
                    note_type=str(op["note_type"]).strip().lower(),
                    title=str(op["title"]).strip(),
                    content=str(op["content"]).strip(),
                    keys=_normalize_str_list(op.get("keys")),
                    tags=_normalize_str_list(op.get("tags")),
                    fact_key=str(op.get("fact_key") or "").strip() or None,
                    evidence_level=op["evidence_level"],
                    source_refs=op["source_refs"],
                    trusted_source=op["_trusted_source"],
                    trusted_domain=op["_trusted_domain"],
                    today_str=today_str,
                )
                atomic_write_text(target, rendered)
                post_write_hashes[rel] = sha256_text(rendered)
                touched_non_diary = True

            elif op_type == "project_update":
                target = op["_resolved_target"]
                existing = target.read_text(encoding="utf-8") if target.exists() else None
                if existing is not None and _extract_managed_block(existing) == op["managed_content"]:
                    skipped_dedupe += 1
                    op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                title = str(op.get("title") or op["project"])
                default_header = (
                    f"---\ntype: project\ntitle: {json.dumps(title, ensure_ascii=False)}\n"
                    f"proactive_owned: true\nstatus: active\n---\n\n# {title}\n\n"
                )
                merged = _merge_managed_block(
                    existing,
                    default_header=default_header,
                    managed_body=op["managed_content"],
                )
                atomic_write_text(target, merged)
                post_write_hashes[rel] = sha256_text(merged)
                touched_non_diary = True
                touched_project = True

            elif op_type == "hot_update":
                target = op["_resolved_target"]
                desired_hot = str(op["content"]).strip() + "\n"
                if target.exists() and target.read_text(encoding="utf-8") == desired_hot:
                    skipped_dedupe += 1
                    op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target, desired_hot)
                post_write_hashes[rel] = sha256_text(desired_hot)
                touched_non_diary = True

            elif op_type == "handoff_upsert":
                h_data = read_handoff(config)
                existing_task = next(
                    (
                        t
                        for t in (h_data.get("tasks") or [])
                        if isinstance(t, dict) and str(t.get("task_key") or "") == op["task_key"]
                    ),
                    None,
                )
                desired_steps = list(op.get("next_steps") or [])
                desired_files = list(op.get("touched_files") or [])
                if (
                    existing_task is not None
                    and str(existing_task.get("status") or "") == op["status"]
                    and str(existing_task.get("summary") or "") == op["summary"]
                    and list(existing_task.get("next_steps") or []) == desired_steps
                    and list(existing_task.get("touched_files") or []) == desired_files
                ):
                    skipped_dedupe += 1
                    op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                    continue
                upsert_handoff(
                    config,
                    task_key=op["task_key"],
                    status=op["status"],
                    summary=op["summary"],
                    next_steps=op.get("next_steps"),
                    touched_files=op.get("touched_files"),
                    updated_by="proactive",
                )
                touched_non_diary = True

            store.record_applied_op(
                op_id=op_id,
                job_id=job.job_id,
                job_idempotency_key=job.idempotency_key,
                op_type=op_type,
                dedupe_key=dedupe_key,
                target_path=rel,
                target_date=op.get("_resolved_date"),
                source_refs=op["source_refs"],
            )
            applied_count += 1
            op_records.append({"op_id": op_id, "op_type": op_type, "status": "applied", "target_rel": rel})

        if touched_project:
            _regenerate_project_overview(config)

        reindexed = False
        if touched_non_diary:
            reindex(config)
            reindexed = True

    if applied_count > 0:
        try:
            from agentbrain.agy_rules import sync_agy_rules

            sync_agy_rules(config)
        except Exception:
            pass

    final_outcome = "applied" if applied_count > 0 else "no_change"
    return ApplySummary(
        outcome=final_outcome,
        applied_count=applied_count,
        skipped_dedupe_count=skipped_dedupe,
        reindexed=reindexed,
        operations=op_records,
        post_write_hashes=post_write_hashes,
    )
