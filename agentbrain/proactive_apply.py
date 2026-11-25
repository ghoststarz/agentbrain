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
from agentbrain.diary import (
    DiaryGateError,
    append_diary,
    normalize_diary_author,
    normalize_diary_kind,
    strip_wander_prefix,
)
from agentbrain.handoff import handoff_path, read_handoff, upsert_handoff
from agentbrain.index import reindex
from agentbrain.proactive import ClaimedJob, LeaseMismatchError, ProactiveStore, StoredEvent
from agentbrain.proactive_clock import Clock, SystemClock, ensure_utc
from agentbrain.retrieve import search_notes

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
_TITLE_SEP_RE = re.compile(r'[：:，,（）()、/\\*?"<>|\x00-\x1f\s]+')
_MULTI_DASH_RE = re.compile(r"-{2,}")
_EVIDENCE_LEVELS = frozenset({"report", "delivered", "verified"})
_EVIDENCE_RANK: dict[str, int] = {"report": 0, "delivered": 1, "verified": 2}
_SEQ_PREFIX_BY_TYPE: dict[str, str] = {"fact": "FACT", "decision": "ADR", "project": "PROJ"}
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_DIARY_LINE_RE = re.compile(r"^-\s*\[\d{2}:\d{2}:\d{2}\](?:\s*\([^)]*\))?\s+(.*)$")
_PROJECT_EV_LINE_RE = re.compile(r"(?im)^\s*(?:evidence_level|verification_status)\s*:\s*.*$")
_SECTION_KEY_COMMENT_RE = re.compile(r"^<!--\s*section_key:\s*(.+?)\s*-->$")
_DEPRECATED_LINE_PREFIX = "> 已废止（"
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+")

MAX_BATCH_OPS = 25
MAX_LONGTERM_CHARS = 16000
MAX_PROJECT_MANAGED_CHARS = 8000
MAX_HANDOFF_SUMMARY_CHARS = 2000
MAX_DIARY_CHARS = 4000
MAX_SOURCE_REFS = 40
MAX_DEPRECATED_LINES = 2
MAX_DEPRECATED_SNIPPET_CHARS = 200
SECTION_SIMILARITY_THRESHOLD = 0.62
MAX_SUMMARY_CHARS = 120
DERIVED_SUMMARY_CHARS = 100


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
    diary_rate_limited_count: int = 0


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_note_title(title: str) -> str:
    """规范化新建笔记标题：标点与空白统一为单连字符，保留中文，主体截断至 20 字。"""
    cleaned = _TITLE_SEP_RE.sub("-", str(title or "").strip())
    cleaned = _MULTI_DASH_RE.sub("-", cleaned).strip("-.")
    if len(cleaned) > 20:
        cleaned = cleaned[:20].rstrip("-.")
    return cleaned if cleaned else "note"


def _sanitize_filename(name: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("-", str(name or "").strip()).strip().strip(".")
    return cleaned[:80] if cleaned else "note"


def _resolve_note_date_compact(op: dict[str, Any], job: ClaimedJob, clock: Clock) -> str:
    """确定性解析新建笔记日期（YYYYMMDD）：op.event_date -> job.window_id -> 当天本地日期。"""
    for raw in (op.get("event_date"), job.window_id):
        s = str(raw or "").strip()
        if s:
            try:
                return dt.date.fromisoformat(s[:10]).strftime("%Y%m%d")
            except ValueError:
                continue
    return clock.to_local(clock.now_utc()).strftime("%Y%m%d")


def _format_seq(seq: int) -> str:
    """格式化序号：两位补零（07、12），达到 100 起三位（100）。"""
    return f"{max(1, int(seq)):02d}"


def _allocate_next_seq(config: AppConfig, note_type: str) -> int:
    """在写入锁内扫描**本目录**的同前缀文件，取最大序号 + 1。

    只看活目录、不看 90_Archive：归档是「这一条退役了」，编号跟着它一起退役，
    否则归档一份旧 ADR-01 就会让新笔记的编号从 07 起步（实测用户把 Decisions
    重排成 01–04 之后，扫描归档会算出 07）。
    """
    prefix = _SEQ_PREFIX_BY_TYPE.get(str(note_type or "").strip().lower())
    if not prefix:
        return 1
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:-|$)", re.IGNORECASE)
    max_seq = 0

    seq_dir = _LONGTERM_DIRS.get(str(note_type or "").strip().lower())
    if not seq_dir:
        # project 不在 _LONGTERM_DIRS 里（它走 project_update 的专用路径）
        seq_dir = "20_LongTerm/Projects" if str(note_type or "").strip().lower() == "project" else ""
    if not seq_dir:
        return 1
    target_dir = config.profile_dir / seq_dir
    if not target_dir.exists() or target_dir.is_symlink():
        return 1

    scan_paths: list[Path] = []
    scan_paths.extend(target_dir.glob(f"{prefix}-*.md"))
    scan_paths.extend(target_dir.glob(f"{prefix.lower()}-*.md"))

    seen_files: set[Path] = set()
    for md in scan_paths:
        if md in seen_files or md.is_symlink():
            continue
        seen_files.add(md)
        m = pat.match(md.stem)
        if not m:
            continue
        digits = m.group(1)
        val = int(digits)
        # 跳过旧式 8 位日期前缀（YYYYMMDD），避免污染两位/三位序号序列
        if len(digits) == 8 and 19000101 <= val <= 29991231:
            continue
        if val > max_seq:
            max_seq = val
    return max_seq + 1


def _build_new_note_filename(
    note_type: str,
    title: str,
    date_ymd: str = "",
    *,
    seq: int | None = None,
) -> str:
    """按统一命名规范构造新建文件名（fact/decision 使用序号，project 保留中文与空格）。"""
    norm = normalize_note_title(title)
    nt = str(note_type or "").strip().lower()
    if nt == "fact":
        return f"FACT-{_format_seq(seq if seq is not None else 1)}-{norm}.md"
    if nt == "decision":
        return f"ADR-{_format_seq(seq if seq is not None else 1)}-{norm}.md"
    if nt == "entity":
        return f"ENTITY-{norm}.md"
    if nt == "moc":
        return f"MOC-{norm}.md"
    if nt == "project":
        clean = _sanitize_filename(title)
        if clean.upper().startswith("PROJ-"):
            return f"{clean}.md"
        return f"PROJ-{_format_seq(seq if seq is not None else 1)}-{clean}.md"
    if nt == "work":
        return f"WORK-{date_ymd}-{norm}.md"
    return f"{norm}.md"


def _resolve_project_note_rel(config: AppConfig, project: str) -> tuple[str, str]:
    """解析项目页路径：`PROJ-<NN>-<项目名>.md`，按「项目名」复用，换序号不影响命中。

    兼容迁移前的两种旧名：`<项目名>.md` 与 `PROJ-<项目名>.md`。
    """
    clean = _sanitize_filename(project)
    for legacy_name in (f"{clean}.md", f"PROJ-{clean}.md"):
        rel = f"20_LongTerm/Projects/{legacy_name}"
        target = _assert_safe_profile_subpath(config, rel)
        if target.exists():
            return rel, Path(legacy_name).stem

    proj_root = config.profile_dir / "20_LongTerm" / "Projects"
    if proj_root.exists() and not proj_root.is_symlink():
        pat = re.compile(r"^PROJ-(\d+)-(.+)$", re.IGNORECASE)
        best: tuple[int, str] | None = None
        for md in sorted(proj_root.glob("PROJ-*.md")):
            if md.is_symlink():
                continue
            m = pat.match(md.stem)
            if not m or m.group(2).strip() != clean:
                continue
            seq = int(m.group(1))
            if best is None or seq < best[0]:
                best = (seq, md.stem)
        if best is not None:
            return f"20_LongTerm/Projects/{best[1]}.md", best[1]

    stem = f"PROJ-{_format_seq(_allocate_next_seq(config, 'project'))}-{clean}"
    rel = f"20_LongTerm/Projects/{stem}.md"
    _assert_safe_profile_subpath(config, rel)
    return rel, stem


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
        # 模型常自造词（inferred/probable…）。降级成 report 比让整批作废划算：
        # 一次枚举写错不该丢掉这一批对话的全部结论。
        ev_level = "report"
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


def _find_merge_target_path(config: AppConfig, merge_into: str) -> str | None:
    """按 merge_into（标题或文件名）在 20_LongTerm 下查找目标合并笔记。"""
    raw = str(merge_into or "").strip()
    if not raw:
        return None
    lt_dir = config.profile_dir / "20_LongTerm"
    if not lt_dir.exists() or lt_dir.is_symlink():
        return None
    target_stem = raw[:-3] if raw.lower().endswith(".md") else raw
    norm_target = normalize_note_title(target_stem)
    norm_match: str | None = None

    for md in sorted(lt_dir.rglob("*.md")):
        if md.name == "_Overview.md" or md.is_symlink():
            continue
        try:
            rel = md.relative_to(config.profile_dir).as_posix()
            _assert_safe_profile_subpath(config, rel)
        except Exception:
            continue
        fm, _ = _parse_frontmatter_and_body(md.read_text(encoding="utf-8", errors="replace"))
        fm_title = str(fm.get("title") or "").strip()
        if rel == raw or md.name == raw or md.stem == target_stem or fm_title == target_stem:
            return rel
        if norm_match is None and fm_title and normalize_note_title(fm_title) == norm_target:
            norm_match = rel
    return norm_match


def _find_exact_title_target_path(config: AppConfig, note_type: str, title: str) -> str | None:
    """按标题/stem 精确匹配同目录下的既有笔记（entity/moc 亦匹配目标标准文件名）。"""
    clean_title = str(title or "").strip()
    if not clean_title:
        return None
    sub_dir = config.profile_dir / _LONGTERM_DIRS[note_type]
    if not sub_dir.exists() or sub_dir.is_symlink():
        return None
    sanitized = _sanitize_filename(clean_title)
    expected_name = (
        _build_new_note_filename(note_type, clean_title)
        if note_type in ("entity", "moc")
        else None
    )
    for md in sorted(sub_dir.glob("*.md")):
        if md.name == "_Overview.md" or md.is_symlink():
            continue
        try:
            rel = md.relative_to(config.profile_dir).as_posix()
            _assert_safe_profile_subpath(config, rel)
        except Exception:
            continue
        if md.stem == clean_title or md.stem == sanitized:
            return rel
        if expected_name and md.name == expected_name:
            return rel
        fm, _ = _parse_frontmatter_and_body(md.read_text(encoding="utf-8", errors="replace"))
        if str(fm.get("title") or "").strip() == clean_title:
            return rel
    return None


def _merge_str_list_ordered(
    existing: Any,
    incoming: list[str],
    *,
    limit: int | None = None,
) -> list[str]:
    """合并字符串列表：去重、保序；超限时保留最新 limit 条。"""
    base = _normalize_str_list(existing) if isinstance(existing, list) else []
    seen = set(base)
    out = list(base)
    for item in incoming:
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


def _higher_evidence_level(existing: Any, incoming: str) -> str:
    cur = str(existing or "").strip().lower()
    inc = str(incoming or "report").strip().lower()
    if cur not in _EVIDENCE_RANK:
        return inc if inc in _EVIDENCE_RANK else "report"
    if inc not in _EVIDENCE_RANK:
        return cur
    return inc if _EVIDENCE_RANK[inc] >= _EVIDENCE_RANK[cur] else cur


@dataclass
class _ManagedSection:
    """受管块中的单个 ### 小节，含可选的稳定槽位键 section_key。"""

    heading: str
    body: str
    section_key: str | None = None


def _normalize_section_key(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    # 防止换行或注释结束符破坏 HTML 注释标记
    s = " ".join(s.replace("-->", "").split())
    return s or None


def _normalize_summary(raw: Any, *, max_chars: int = MAX_SUMMARY_CHARS) -> str | None:
    s = " ".join(str(raw or "").split()).strip()
    if not s:
        return None
    if len(s) <= max_chars:
        return s
    return s[: max(1, max_chars - 1)].rstrip() + "…"


def _derive_note_summary(content: str, max_chars: int = DERIVED_SUMMARY_CHARS) -> str:
    """未显式提供 summary 时，从正文派生首句摘要（截断至 max_chars 字）。"""
    first = ""
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("#")
            or line.startswith("<!--")
            or line.startswith("evidence_level:")
            or line.startswith(_DEPRECATED_LINE_PREFIX)
        ):
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if line:
            first = line
            break
    if not first:
        first = " ".join(str(content or "").split()).strip()
    if not first:
        return ""
    m = re.search(r"[。！？!?]|(?<!\d)\.(?=\s|$)", first)
    if m and m.start() > 0:
        first = first[: m.end()].strip()
    first = " ".join(first.split())
    if len(first) <= max_chars:
        return first
    return first[: max(1, max_chars - 1)].rstrip() + "…"


def _title_bigrams(title: str) -> set[str]:
    """对规范化后的标题提取 CJK 二元组（及长度>=2 的 ASCII 词）用于相似度匹配。"""
    norm = normalize_note_title(title).lower()
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(norm):
        if len(run) == 1:
            grams.add(run)
        else:
            for i in range(len(run) - 1):
                grams.add(run[i : i + 2])
    for tok in _ASCII_TOKEN_RE.findall(norm):
        if len(tok) >= 2:
            grams.add(tok)
    return grams


def _title_jaccard(a: str, b: str) -> float:
    ga = _title_bigrams(a)
    gb = _title_bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _split_section_body_and_deprecated(body: str) -> tuple[str, list[str]]:
    """把小节正文拆分为现行正文与末尾的「> 已废止（...）」历史行列表。"""
    active_lines: list[str] = []
    deprecated_lines: list[str] = []
    for raw_line in str(body or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(_DEPRECATED_LINE_PREFIX):
            deprecated_lines.append(stripped)
        else:
            active_lines.append(raw_line.rstrip())
    return "\n".join(active_lines).strip(), deprecated_lines


def _parse_managed_sections(
    managed_text: str | None,
) -> tuple[str, list[_ManagedSection], list[str]]:
    """把受管区块拆分为：前置概要 preamble、有序 ### 小节列表、末尾 ## 关联 链接行列表。

    `## 来源`（会话出处）小节也按链接行处理，和 `## 关联` 合并返回——
    它同样只会在受管块末尾追加，不需要单独的手术。
    """
    if not managed_text or not managed_text.strip():
        return "", [], []
    preamble_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    link_lines: list[str] = []
    mode = "preamble"

    for raw_line in managed_text.splitlines():
        stripped = raw_line.strip()
        if re.match(r"^##\s+关联\s*$", stripped) or re.match(r"^##\s+来源\s*$", stripped):
            mode = "links"
            continue
        m_sec = re.match(r"^###\s+(.+?)\s*$", stripped)
        if m_sec and mode != "links":
            mode = "section"
            sections.append((m_sec.group(1).strip(), []))
            continue
        if mode == "links":
            if stripped:
                link_lines.append(raw_line.rstrip())
        elif mode == "section":
            sections[-1][1].append(raw_line.rstrip())
        else:
            preamble_lines.append(raw_line.rstrip())

    preamble = "\n".join(preamble_lines).strip()
    clean_sections: list[_ManagedSection] = []
    for h, lines in sections:
        sec_key: str | None = None
        body_lines: list[str] = []
        checked_first = False
        for ln in lines:
            if not checked_first and not ln.strip():
                continue
            if not checked_first:
                checked_first = True
                m_key = _SECTION_KEY_COMMENT_RE.match(ln.strip())
                if m_key:
                    sec_key = _normalize_section_key(m_key.group(1))
                    continue
            body_lines.append(ln)
        clean_sections.append(
            _ManagedSection(
                heading=h,
                body="\n".join(body_lines).strip(),
                section_key=sec_key,
            )
        )
    return preamble, clean_sections, link_lines


def _find_matching_section_index(
    sections: list[_ManagedSection],
    heading: str,
    section_key: str | None,
) -> int | None:
    """严格按顺序定位目标小节：1. section_key 相等 -> 2. 标题逐字相等 -> 3. 标题二元组 Jaccard >= 0.62。"""
    clean_heading = heading.strip()
    clean_key = _normalize_section_key(section_key)

    if clean_key:
        for idx, sec in enumerate(sections):
            if sec.section_key and sec.section_key == clean_key:
                return idx

    for idx, sec in enumerate(sections):
        if sec.heading.strip() == clean_heading:
            return idx

    best_idx: int | None = None
    best_sim = 0.0
    for idx, sec in enumerate(sections):
        sim = _title_jaccard(sec.heading, clean_heading)
        if sim >= SECTION_SIMILARITY_THRESHOLD and sim > best_sim:
            best_sim = sim
            best_idx = idx
    return best_idx


def _upsert_managed_section(
    sections: list[_ManagedSection],
    heading: str,
    body: str,
    *,
    section_key: str | None = None,
    today_str: str = "",
) -> list[_ManagedSection]:
    """按三层规则匹配小节并就地替换；旧正文与新正文不同时追加「> 已废止」行（最多保留 2 条）。"""
    clean_heading = heading.strip()
    clean_body = body.strip()
    clean_key = _normalize_section_key(section_key)
    eff_today = today_str or dt.date.today().isoformat()

    match_idx = _find_matching_section_index(sections, clean_heading, clean_key)
    updated = list(sections)
    if match_idx is None:
        updated.append(_ManagedSection(heading=clean_heading, body=clean_body, section_key=clean_key))
        return updated

    old_sec = sections[match_idx]
    old_active, deprecated_lines = _split_section_body_and_deprecated(old_sec.body)
    norm_old = " ".join(old_active.split())
    norm_new = " ".join(clean_body.split())
    if norm_old and norm_old != norm_new:
        snippet = norm_old[:MAX_DEPRECATED_SNIPPET_CHARS]
        deprecated_lines.append(f"{_DEPRECATED_LINE_PREFIX}{eff_today}）：{snippet}")
    deprecated_lines = deprecated_lines[-MAX_DEPRECATED_LINES:]

    if deprecated_lines:
        merged_body = f"{clean_body}\n\n" + "\n".join(deprecated_lines)
    else:
        merged_body = clean_body

    resolved_key = clean_key if clean_key else old_sec.section_key
    updated[match_idx] = _ManagedSection(
        heading=clean_heading,
        body=merged_body,
        section_key=resolved_key,
    )
    return updated


def _clean_link_title(raw: str) -> str:
    s = str(raw or "").strip().strip("[]").strip()
    if s.lower().endswith(".md"):
        s = s[:-3].strip()
    return s


def _collect_candidate_links(
    config: AppConfig,
    *,
    op_links: list[str],
    title: str,
    content: str,
    self_rel: str,
    self_stem: str,
    extra_excludes: set[str] | None = None,
) -> tuple[list[str], set[str]]:
    """汇总 op 显式 links 与检索自动命中的候选标题（排除自身）。"""
    excludes = {x for x in (title.strip(), self_stem.strip()) if x}
    if extra_excludes:
        excludes.update(x.strip() for x in extra_excludes if x and x.strip())

    candidates: list[str] = []
    for lk in op_links:
        clean = _clean_link_title(lk)
        if clean and clean not in excludes and clean not in candidates:
            candidates.append(clean)

    query = f"{title} {content[:60]}".strip()
    if query:
        try:
            res = search_notes(config, query, k_override=2)
            for hit in res.get("hits") or []:
                hit_path = str(hit.get("path") or "").replace("\\", "/")
                hit_title = _clean_link_title(str(hit.get("title") or ""))
                if not hit_title:
                    continue
                if hit_path == self_rel or Path(hit_path).stem == self_stem:
                    continue
                if hit_title in excludes or hit_title in candidates:
                    continue
                candidates.append(hit_title)
        except Exception:
            pass

    return candidates, excludes


def _merge_link_lines(
    existing_link_lines: list[str],
    candidate_links: list[str],
    *,
    exclude_names: set[str],
) -> list[str]:
    """保留所有已有链接行（含手写），仅补充不重复的 - related_to [[标题]]。"""
    out = list(existing_link_lines)
    seen_targets: set[str] = set()
    for line in existing_link_lines:
        for m in _WIKILINK_RE.finditer(line):
            target = m.group(1).strip()
            if target:
                seen_targets.add(target)

    for raw in candidate_links:
        clean = _clean_link_title(raw)
        if not clean or clean in exclude_names or clean in seen_targets:
            continue
        out.append(f"- related_to [[{clean}]]")
        seen_targets.add(clean)
    return out


def _render_managed_sections(
    preamble: str,
    sections: list[_ManagedSection],
    link_lines: list[str],
    source_lines: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.strip())
    for sec in sections:
        header = f"### {sec.heading}"
        if sec.section_key:
            header = f"{header}\n<!-- section_key: {sec.section_key} -->"
        body_clean = sec.body.strip()
        parts.append(f"{header}\n\n{body_clean}" if body_clean else header)
    # 解析时「来源」行会随「关联」一起回来，这里按前缀把它们分回各自的小节
    derived_source = [ln for ln in link_lines if "来源：" in ln]
    derived_links = [ln for ln in link_lines if "来源：" not in ln]
    all_sources = list(source_lines or [])
    seen_sources: set[str] = set()
    for ln in all_sources:
        m_sid = _SESSION_ID_RE.search(ln)
        seen_sources.add(m_sid.group(0).lower() if m_sid else ln.strip())
    for ln in derived_source:
        m_sid = _SESSION_ID_RE.search(ln)
        key = m_sid.group(0).lower() if m_sid else ln.strip()
        if key in seen_sources:
            continue
        all_sources.append(ln)
        seen_sources.add(key)
    if derived_links:
        parts.append("## 关联\n" + "\n".join(derived_links))
    if all_sources:
        parts.append("## 来源\n" + "\n".join(all_sources))
    return "\n\n".join(parts).strip()


_SESSION_ID_RE = re.compile(r"local-([0-9a-f]{16,})", re.IGNORECASE)


def _extract_session_sources(source_refs: Any) -> list[tuple[str, str]]:
    """从 source_refs 中提取去重的 (client, 完整 session_id) 列表。

    例如 `hermes:local-<64hex>:turn:19` -> `("hermes", "local-<64hex>")`。
    """
    found: list[tuple[str, str]] = []
    seen_sids: set[str] = set()
    refs = source_refs if isinstance(source_refs, list) else []
    for ref in refs:
        raw = str(ref or "").strip()
        m = _SESSION_ID_RE.search(raw)
        if not m:
            continue
        sid = f"local-{m.group(1).lower()}"
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        client = "hermes"
        if ":" in raw:
            prefix = raw.split(":", 1)[0].strip()
            if prefix and not prefix.lower().startswith("local-"):
                client = display_client_id(prefix) or "hermes"
        found.append((client, sid))
    return found


def _format_source_lines(
    session_sources: list[tuple[str, str]] | None,
    existing: list[str] | None = None,
) -> list[str]:
    """合并「来源」小节的纯文本行（不建 wikilink），按完整 session_id 去重。"""
    out = list(existing or [])
    seen_sids: set[str] = set()
    seen_lines: set[str] = set()
    for line in out:
        seen_lines.add(line.strip())
        m = _SESSION_ID_RE.search(line)
        if m:
            seen_sids.add(m.group(0).lower())
    for client, sid in session_sources or []:
        norm_sid = sid.lower()
        line = f"- 来源：{client} 会话 {norm_sid}"
        if norm_sid in seen_sids or line in seen_lines:
            continue
        out.append(line)
        seen_sids.add(norm_sid)
        seen_lines.add(line)
    return out


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
    candidate_links: list[str] | None = None,
    exclude_link_names: set[str] | None = None,
    session_sources: list[tuple[str, str]] | None = None,
    as_section: bool = False,
    section_key: str | None = None,
    summary: str | None = None,
) -> str:
    """生成或更新长期笔记：新建写正文+关联，已有文件在受管块内追加/替换 ### 小节并合并 frontmatter。"""
    excl = set(exclude_link_names or ())
    excl.add(title.strip())
    links_in = list(candidate_links or [])
    clean_section_key = _normalize_section_key(section_key)
    clean_summary = _normalize_summary(summary)

    if existing_text is None:
        fm: dict[str, Any] = {
            "type": note_type,
            "title": title,
            "summary": clean_summary or _derive_note_summary(content),
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
            fm["keys"] = _merge_str_list_ordered([], keys)
        if tags:
            fm["tags"] = _merge_str_list_ordered([], tags)
        if source_refs:
            fm["source_refs"] = _merge_str_list_ordered([], source_refs, limit=MAX_SOURCE_REFS)
        link_lines = _merge_link_lines([], links_in, exclude_names=excl)
        source_lines = _format_source_lines(session_sources)
        if as_section or clean_section_key:
            managed_body = _render_managed_sections(
                "",
                [_ManagedSection(heading=title, body=content, section_key=clean_section_key)],
                link_lines,
                source_lines,
            )
        else:
            managed_body = _render_managed_sections(content, [], link_lines, source_lines)
        header = f"{_dump_frontmatter(fm)}\n# {title}\n\n"
        return _merge_managed_block(None, default_header=header, managed_body=managed_body)

    fm, body = _parse_frontmatter_and_body(existing_text)
    if not fm.get("type"):
        fm["type"] = note_type
    if not fm.get("title"):
        fm["title"] = title
    if clean_summary:
        fm["summary"] = clean_summary
    elif not str(fm.get("summary") or "").strip():
        fm["summary"] = _derive_note_summary(content)
    if not fm.get("created"):
        fm["created"] = today_str
    fm["updated"] = today_str
    if not fm.get("source"):
        fm["source"] = trusted_source
    if not fm.get("domain"):
        fm["domain"] = trusted_domain
    if not fm.get("status"):
        fm["status"] = "active"
    fm["evidence_level"] = _higher_evidence_level(fm.get("evidence_level"), evidence_level)
    if fact_key:
        fm["fact_key"] = fact_key
    merged_keys = _merge_str_list_ordered(fm.get("keys"), keys)
    if merged_keys:
        fm["keys"] = merged_keys
    merged_tags = _merge_str_list_ordered(fm.get("tags"), tags)
    if merged_tags:
        fm["tags"] = merged_tags
    merged_refs = _merge_str_list_ordered(fm.get("source_refs"), source_refs, limit=MAX_SOURCE_REFS)
    if merged_refs:
        fm["source_refs"] = merged_refs

    existing_managed = _extract_managed_block(body)
    preamble, sections, existing_links = _parse_managed_sections(existing_managed)
    sections = _upsert_managed_section(
        sections,
        title,
        content,
        section_key=clean_section_key,
        today_str=today_str,
    )
    link_lines = _merge_link_lines(existing_links, links_in, exclude_names=excl)
    source_lines = _format_source_lines(session_sources)
    managed_body = _render_managed_sections(preamble, sections, link_lines, source_lines)

    merged_body = _merge_managed_block(
        body.lstrip("\r\n"),
        default_header=f"# {fm.get('title') or title}\n\n",
        managed_body=managed_body,
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
        if not managed:
            continue
        # 只取一句话概要：跳过 evidence_level 行与标题行，取第一段实质内容的首句。
        summary = ""
        for raw_line in managed.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("evidence_level:"):
                continue
            if line.startswith("##") or line.startswith("- "):
                line = line.lstrip("- ").strip()
            summary = line
            break
        for sep in ("。", "；", "！", "？"):
            idx = summary.find(sep)
            if 0 < idx:
                summary = summary[: idx + 1]
                break
        summary = summary[:120].strip()
        entries.append((md_file.stem, summary))

    if not entries:
        return None

    managed_lines: list[str] = []
    for name, summary in entries:
        managed_lines.append(f"## [[{name}]]")
        managed_lines.append("")
        managed_lines.append(summary or "（暂无摘要）")
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
    *,
    config: AppConfig | None = None,
) -> dt.datetime:
    """日记日期严格限定在源事件本地日期或夜间窗口日期范围内，绝不接受模型任意日期。

    时间戳别写死 22:00：夜间任务实际在 22:30 落盘，写死会让日记时间和事实打架。
    当天就记真实落盘时间；补写往日才回落到夜间调度时刻（取配置里的 hour/minute）。
    """
    nightly_h, nightly_m = 22, 30
    if config is not None:
        proactive = config.memory.proactive
        nightly_h = int(getattr(proactive, "nightly_hour", nightly_h) or nightly_h)
        nightly_m = int(getattr(proactive, "nightly_minute", nightly_m) or 0)
    now_local = clock.to_local(clock.now_utc()).replace(tzinfo=None)
    allowed_dates: set[str] = set()
    for ev in matched_events:
        utc_dt = ensure_utc(dt.datetime.fromisoformat(ev.occurred_at_utc))
        allowed_dates.add(clock.to_local(utc_dt).strftime("%Y-%m-%d"))
    if job.job_type in ("nightly", "nightly_diary") and job.window_id:
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
        return (
            now_local
            if parsed_d == now_local.date()
            else dt.datetime(parsed_d.year, parsed_d.month, parsed_d.day, nightly_h, nightly_m, 0)
        )

    if matched_events:
        utc_dt = ensure_utc(dt.datetime.fromisoformat(matched_events[0].occurred_at_utc))
        return clock.to_local(utc_dt).replace(tzinfo=None)

    if job.window_id:
        win_date = dt.date.fromisoformat(job.window_id)
        return (
            now_local
            if win_date == now_local.date()
            else dt.datetime(win_date.year, win_date.month, win_date.day, nightly_h, nightly_m, 0)
        )

    return clock.to_local(clock.now_utc()).replace(tzinfo=None)


def _diary_entry_body(content: str, *, kind: str = "daily") -> str:
    clean = " ".join(strip_wander_prefix(content).split())
    if kind == "wander" and clean:
        return f"[wander] {clean}"
    return clean


def _diary_contains_exact_entry(
    config: AppConfig,
    ledger: str,
    date_str: str,
    content: str,
    *,
    kind: str = "daily",
) -> bool:
    """精确行正文比对（绝不使用子串匹配），用于崩溃恢复幂等。"""
    sub = "Public" if ledger == "public" else "Private"
    target = config.profile_dir / "30_Diary" / sub / f"{date_str}.md"
    if not target.exists():
        return False
    norm_new = _diary_entry_body(content, kind=kind)
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
    old_kind: str = "daily",
    new_kind: str = "daily",
    author: str = "",
) -> bool:
    """在日记文件中就地更新同一证据来源的自动条目（精确匹配旧正文，绝不误改其他条目）。"""
    if not target.exists():
        return False
    norm_old = _diary_entry_body(old_content, kind=old_kind)
    clean_new = strip_wander_prefix(new_content)
    if not clean_new:
        return False
    clean_author = normalize_diary_author(author)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    for idx, line in enumerate(lines):
        m = _DIARY_LINE_RE.match(line.strip())
        if m and " ".join(m.group(1).split()) == norm_old:
            time_str = now_dt.strftime("%H:%M:%S")
            kind_tag = " [wander]" if new_kind == "wander" else ""
            lines[idx] = f"- [{time_str}] ({clean_author}){kind_tag} {clean_new}\n"
            atomic_write_text(target, "".join(lines))
            return True
    return False


def _existing_diary_timestamps(target: Path) -> set[str]:
    """读取目标日记文件中已出现的 HH:MM:SS 时间戳集合，用于避免多条写入时间戳撞车。"""
    if not target.exists():
        return set()
    used: set[str] = set()
    ts_re = re.compile(r"^-\s*\[(\d{2}:\d{2}:\d{2})\]")
    try:
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            m = ts_re.match(line.strip())
            if m:
                used.add(m.group(1))
    except OSError:
        pass
    return used


def _allocate_unique_diary_dt(base_dt: dt.datetime, used_times: set[str]) -> dt.datetime:
    """确保同日同一账本内多条日记的时间戳不重复（撞车则顺延 1 秒，不跨天）。"""
    cur = base_dt
    day_end = base_dt.replace(hour=23, minute=59, second=59)
    while cur.strftime("%H:%M:%S") in used_times and cur < day_end:
        cur = cur + dt.timedelta(seconds=1)
    used_times.add(cur.strftime("%H:%M:%S"))
    return cur


def _derive_op_id(job: ClaimedJob, segment_index: int, op_type: str, dedupe_key: str) -> str:
    digest = sha256_text(f"{op_type}:{dedupe_key}")[:16]
    return f"{job.idempotency_key}:{int(segment_index)}:{digest}"


def _derive_dedupe_key(op: dict[str, Any]) -> str:
    op_type = str(op["op_type"])
    if op_type == "diary_append":
        ledger = str(op.get("ledger") or "").strip().lower()
        kind = str(op.get("kind") or "daily").strip().lower()
        date_hint = str(op.get("_resolved_date") or "")
        canonical_refs = op.get("_canonical_source_refs") or []
        refs_digest = sha256_text("|".join(canonical_refs))[:24]
        base_key = f"diary:{ledger}:{date_hint}:refs:{refs_digest}"
        if kind != "daily":
            base_key = f"{base_key}:kind:{kind}"
        slot = int(op.get("_diary_slot") or 0)
        if slot > 0:
            return f"{base_key}:slot:{slot}"
        return base_key
    if op_type == "longterm_upsert":
        fact_key = str(op.get("fact_key") or "").strip()
        slot = fact_key or str(op.get("_dedupe_slot") or op.get("_resolved_rel") or "")
        content_hash = sha256_text(" ".join(str(op.get("content") or "").split()))[:16]
        extra = []
        sec_key = _normalize_section_key(op.get("section_key"))
        if sec_key:
            extra.append(f"sk:{sec_key}")
        summary_val = _normalize_summary(op.get("summary"))
        if summary_val:
            extra.append(f"sm:{sha256_text(summary_val)[:8]}")
        suffix = (":" + ":".join(extra)) if extra else ""
        return f"slot:{slot}:{content_hash}{suffix}"
    if op_type == "project_update":
        project = _sanitize_filename(str(op.get("project") or op.get("title") or ""))
        block_hash = sha256_text(str(op.get("managed_content") or "").strip())[:16]
        summary_val = _normalize_summary(op.get("summary"))
        suffix = f":sm:{sha256_text(summary_val)[:8]}" if summary_val else ""
        return f"project:{project}:{block_hash}{suffix}"
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
    segment_index: int = 0,
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
    diary_slot_counts: dict[tuple[str, str, str, str], int] = {}

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
        diary_kind = "daily"
        if op_type == "diary_append":
            try:
                diary_kind = normalize_diary_kind(op.get("kind"))
            except ValueError as exc:
                raise BatchValidationError(f"operation[{idx}] {exc}") from exc
            op["kind"] = diary_kind

        if (
            job.job_type in ("nightly", "nightly_diary")
            and op_type == "diary_append"
            and (not events or diary_kind == "wander")
        ):
            raw_refs = _normalize_str_list(op.get("source_refs"))
            if raw_refs:
                source_refs, canonical_refs, matched_events = _resolve_source_refs(
                    raw_refs,
                    events,
                    idx,
                    op_type,
                )
            else:
                source_refs, canonical_refs, matched_events = [], [], []
        else:
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
            scope = str(op.get("scope") or "global").strip().lower()
            if scope not in ("global", "project"):
                raise BatchValidationError(f"operation[{idx}] 非法 scope: {scope!r}")
            op["scope"] = scope
            op["links"] = _normalize_str_list(op.get("links"))
            op["section_key"] = _normalize_section_key(op.get("section_key"))
            op["summary"] = _normalize_summary(op.get("summary"))
            fact_key = str(op.get("fact_key") or "").strip() or None
            merge_into = str(op.get("merge_into") or "").strip() or None
            op["_merge_into"] = merge_into
            expected_hash = str(op.get("expected_hash") or "").strip()

            if scope == "global" and note_type == "project":
                # 模型有时把项目页标题直接写成 `PROJ-…`（旧命名习惯）。不纠正的话
                # 会绕开「按项目名复用」的逻辑，在 Projects 下另建一个同义文件。
                derived = re.sub(r"^PROJ-(\d+-)?", "", str(op.get("title") or ""), flags=re.IGNORECASE).strip()
                if derived:
                    op["project"] = derived
                    scope = "project"
                    op["scope"] = "project"

            if scope == "project":
                project_raw = str(op.get("project") or "").strip()
                if not project_raw:
                    raise BatchValidationError(
                        f"operation[{idx}] longterm_upsert scope=project 时必须提供 project"
                    )
                rel, proj_stem = _resolve_project_note_rel(config, project_raw)
                target = _assert_safe_profile_subpath(config, rel)
                op["_project_raw"] = project_raw
                op["_project_stem"] = proj_stem
                op["_needs_seq_alloc"] = False
                op["_resolved_rel"] = rel
                op["_resolved_target"] = target
                op["_dedupe_slot"] = f"{rel}#{title}"
                _preview_dk = _derive_dedupe_key(op)
                _preview_oid = str(op.get("op_id") or "").strip() or _derive_op_id(job, segment_index, op_type, _preview_dk)
                if not store.is_op_applied(_preview_oid):
                    req_hash = _check_hash_conflict(
                        rel=rel,
                        target=target,
                        expected_hash=expected_hash,
                        snapshot_hashes=snapshot_hashes,
                        initial_snapshot_hashes=initial_snapshot_hashes,
                        trusted_post_write_hashes=trusted_post_write_hashes,
                    )
                    pre_lock_checks.append((target, rel, req_hash))
            else:
                # 严格按顺序解析：1. fact_key -> 2. merge_into -> 3. 精确标题/stem -> 4. 新建
                # 槽位键与文件是否已存在解耦，保证同一 segment 重跑时 dedupe_key/op_id 稳定一致。
                stable_slot = (
                    f"{note_type}:{normalize_note_title(merge_into)}#{normalize_note_title(title)}"
                    if merge_into
                    else f"{note_type}:{normalize_note_title(title)}"
                )
                op["_dedupe_slot"] = stable_slot
                _preview_dk = _derive_dedupe_key(op)
                _preview_oid = str(op.get("op_id") or "").strip() or _derive_op_id(job, segment_index, op_type, _preview_dk)
                _already_applied = store.is_op_applied(_preview_oid)

                existing_rel = _find_existing_fact_key_path(config, fact_key) if fact_key else None
                if existing_rel is None and merge_into:
                    existing_rel = _find_merge_target_path(config, merge_into)
                if existing_rel is None:
                    existing_rel = _find_exact_title_target_path(config, note_type, title)

                if existing_rel is not None:
                    rel = existing_rel
                    target = _assert_safe_profile_subpath(config, rel)
                    if not _already_applied:
                        req_hash = _check_hash_conflict(
                            rel=rel,
                            target=target,
                            expected_hash=expected_hash,
                            snapshot_hashes=snapshot_hashes,
                            initial_snapshot_hashes=initial_snapshot_hashes,
                            trusted_post_write_hashes=trusted_post_write_hashes,
                        )
                        pre_lock_checks.append((target, rel, req_hash))
                    op["_needs_seq_alloc"] = False
                    op["_resolved_rel"] = rel
                    op["_resolved_target"] = target
                elif note_type in _SEQ_PREFIX_BY_TYPE:
                    if expected_hash and not _already_applied:
                        raise ConflictError(
                            f"创建冲突：新建 {note_type} 笔记 {title!r} 不存在但提供了 expected_hash={expected_hash[:12]}"
                        )
                    preview_seq = _allocate_next_seq(config, note_type)
                    preview_rel = f"{_LONGTERM_DIRS[note_type]}/{_build_new_note_filename(note_type, title, seq=preview_seq)}"
                    _assert_safe_profile_subpath(config, preview_rel)
                    op["_needs_seq_alloc"] = True
                    op["_resolved_rel"] = preview_rel
                    op["_resolved_target"] = config.profile_dir / preview_rel
                else:
                    date_ymd = _resolve_note_date_compact(op, job, eff_clock)
                    rel = f"{_LONGTERM_DIRS[note_type]}/{_build_new_note_filename(note_type, title, date_ymd)}"
                    target = _assert_safe_profile_subpath(config, rel)
                    if not _already_applied:
                        req_hash = _check_hash_conflict(
                            rel=rel,
                            target=target,
                            expected_hash=expected_hash,
                            snapshot_hashes=snapshot_hashes,
                            initial_snapshot_hashes=initial_snapshot_hashes,
                            trusted_post_write_hashes=trusted_post_write_hashes,
                        )
                        pre_lock_checks.append((target, rel, req_hash))
                    op["_needs_seq_alloc"] = False
                    op["_resolved_rel"] = rel
                    op["_resolved_target"] = target

            op["evidence_level"] = _resolve_evidence_level(op.get("evidence_level"), matched_events, idx)
            trusted_source, trusted_domain = _trusted_source_and_domain(config, matched_events)
            op["_trusted_source"] = trusted_source
            op["_trusted_domain"] = trusted_domain

        elif op_type == "project_update":
            project = str(op.get("project") or op.get("title") or "").strip()
            raw_managed = str(op.get("managed_content") or op.get("content") or "").strip()
            if not project or not raw_managed:
                raise BatchValidationError(f"operation[{idx}] project_update 缺少 project 或 managed_content")
            if len(raw_managed) > MAX_PROJECT_MANAGED_CHARS:
                raise BatchValidationError(
                    f"operation[{idx}] project_update 超出字符上限 ({len(raw_managed)} > {MAX_PROJECT_MANAGED_CHARS})"
                )
            rel, slug = _resolve_project_note_rel(config, project)
            target = _assert_safe_profile_subpath(config, rel)
            expected_hash = str(op.get("expected_hash") or "").strip()
            ev_level = _resolve_evidence_level(op.get("evidence_level"), matched_events, idx)
            op["project"] = slug
            op["_project_raw"] = project
            op["evidence_level"] = ev_level
            op["managed_content"] = _format_project_managed_body(raw_managed, ev_level)
            op["links"] = _normalize_str_list(op.get("links"))
            op["summary"] = _normalize_summary(op.get("summary"))
            op["_raw_managed_content"] = raw_managed
            op["_resolved_rel"] = rel
            op["_resolved_target"] = target
            _preview_dk = _derive_dedupe_key(op)
            _preview_oid = str(op.get("op_id") or "").strip() or _derive_op_id(job, segment_index, op_type, _preview_dk)
            if not store.is_op_applied(_preview_oid):
                req_hash = _check_hash_conflict(
                    rel=rel,
                    target=target,
                    expected_hash=expected_hash,
                    snapshot_hashes=snapshot_hashes,
                    initial_snapshot_hashes=initial_snapshot_hashes,
                    trusted_post_write_hashes=trusted_post_write_hashes,
                )
                pre_lock_checks.append((target, rel, req_hash))

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
            op["_resolved_rel"] = rel
            op["_resolved_target"] = target
            _preview_dk = _derive_dedupe_key(op)
            _preview_oid = str(op.get("op_id") or "").strip() or _derive_op_id(job, segment_index, op_type, _preview_dk)
            if not store.is_op_applied(_preview_oid):
                req_hash = _check_hash_conflict(
                    rel=rel,
                    target=target,
                    expected_hash=expected_hash,
                    snapshot_hashes=snapshot_hashes,
                    initial_snapshot_hashes=initial_snapshot_hashes,
                    trusted_post_write_hashes=trusted_post_write_hashes,
                )
                pre_lock_checks.append((target, rel, req_hash))

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
            content = strip_wander_prefix(str(op.get("content") or ""))
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
            target_dt = _resolve_diary_datetime(op, job, matched_events, eff_clock, config=config)
            date_str = target_dt.strftime("%Y-%m-%d")
            rel = f"30_Diary/{'Public' if ledger == 'public' else 'Private'}/{date_str}.md"
            _assert_no_symlink_escape(config, rel)
            op["ledger"] = ledger
            op["content"] = content
            op["_resolved_dt"] = target_dt
            op["_resolved_date"] = date_str
            op["_resolved_rel"] = rel
            refs_digest = sha256_text("|".join(canonical_refs))[:24]
            slot_key = (ledger, op["kind"], date_str, refs_digest)
            op["_diary_slot"] = diary_slot_counts.get(slot_key, 0)
            diary_slot_counts[slot_key] = op["_diary_slot"] + 1

        dedupe_key = _derive_dedupe_key(op)
        op_id = str(op.get("op_id") or "").strip() or _derive_op_id(job, segment_index, op_type, dedupe_key)
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
    diary_rate_limited = 0
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

        diary_used_times: dict[str, set[str]] = {}
        try:
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
                    op_kind = str(op.get("kind") or "daily")
                    diary_target = _assert_no_symlink_escape(config, rel)
                    if rel not in diary_used_times:
                        diary_used_times[rel] = _existing_diary_timestamps(diary_target)
                    journal = store.get_adapter_cursor("diary_journal", dedupe_key)
                    norm_new = " ".join(op["content"].split())
                    if journal is not None:
                        prev_content = str(journal.get("content") or "")
                        prev_kind = str(journal.get("kind") or "daily")
                        if " ".join(strip_wander_prefix(prev_content).split()) == norm_new and prev_kind == op_kind:
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
                        eff_dt = _allocate_unique_diary_dt(op["_resolved_dt"], diary_used_times[rel])
                        if _replace_diary_auto_entry(
                            diary_target,
                            prev_content,
                            op["content"],
                            now_dt=eff_dt,
                            old_kind=prev_kind,
                            new_kind=op_kind,
                            author=job.client or "",
                        ):
                            store.set_adapter_cursor(
                                "diary_journal",
                                dedupe_key,
                                {
                                    "target_path": rel,
                                    "target_date": op["_resolved_date"],
                                    "content": op["content"],
                                    "kind": op_kind,
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

                    if _diary_contains_exact_entry(
                        config,
                        op["ledger"],
                        op["_resolved_date"],
                        op["content"],
                        kind=op_kind,
                    ):
                        store.set_adapter_cursor(
                            "diary_journal",
                            dedupe_key,
                            {
                                "target_path": rel,
                                "target_date": op["_resolved_date"],
                                "content": op["content"],
                                "kind": op_kind,
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

                    eff_dt = _allocate_unique_diary_dt(op["_resolved_dt"], diary_used_times[rel])
                    diary_res = append_diary(
                        config,
                        ledger=op["ledger"],
                        content=op["content"],
                        author=normalize_diary_author(job.client or ""),
                        kind=op_kind,
                        can_casual=False,
                        now=eff_dt,
                    )
                    if diary_res.get("skipped_duplicate"):
                        skipped_dedupe += 1
                        op_records.append(
                            {"op_id": op_id, "op_type": op_type, "status": "skipped_duplicate", "target_rel": rel}
                        )
                        continue
                    if diary_res.get("skipped_limit"):
                        skipped_dedupe += 1
                        diary_rate_limited += 1
                        op_records.append(
                            {"op_id": op_id, "op_type": op_type, "status": "skipped_limit", "target_rel": rel}
                        )
                        continue
                    store.set_adapter_cursor(
                        "diary_journal",
                        dedupe_key,
                        {
                            "target_path": rel,
                            "target_date": op["_resolved_date"],
                            "content": op["content"],
                            "kind": op_kind,
                            "op_id": op_id,
                        },
                    )

                elif op_type == "longterm_upsert":
                    note_type = str(op["note_type"]).strip().lower()
                    title = str(op["title"]).strip()
                    content = str(op["content"]).strip()
                    scope = str(op.get("scope") or "global").strip().lower()
                    fact_key = str(op.get("fact_key") or "").strip() or None
                    merge_into = op.get("_merge_into")

                    if op.get("_needs_seq_alloc"):
                        # 写锁内重新解析（避免同批次前序 op 已新建同名/同键文件，并在锁内单调分配序号）
                        locked_rel = _find_existing_fact_key_path(config, fact_key) if fact_key else None
                        if locked_rel is None and merge_into:
                            locked_rel = _find_merge_target_path(config, merge_into)
                        if locked_rel is None:
                            locked_rel = _find_exact_title_target_path(config, note_type, title)
                        if locked_rel is not None:
                            rel = locked_rel
                        else:
                            seq = _allocate_next_seq(config, note_type)
                            rel = f"{_LONGTERM_DIRS[note_type]}/{_build_new_note_filename(note_type, title, seq=seq)}"
                        target = _assert_safe_profile_subpath(config, rel)
                        op["_resolved_rel"] = rel
                        op["_resolved_target"] = target
                    else:
                        target = op["_resolved_target"]

                    existing = target.read_text(encoding="utf-8") if target.exists() else None
                    session_sources = _extract_session_sources(op["source_refs"])
                    if not session_sources:
                        # source_refs 可能是「短事件 id」（如 turn:12），抽不出会话标识；
                        # 这时从命中的事件本身取 session_id，别让「来源」整节凭空消失。
                        seen_sids: set[str] = set()
                        for _ev in op.get("_matched_events") or []:
                            _sid = str(getattr(_ev, "session_id", "") or "").strip()
                            if not _sid or _sid in seen_sids:
                                continue
                            seen_sids.add(_sid)
                            session_sources.append(
                                (display_client_id(str(getattr(_ev, "client", ""))) or "hermes", _sid)
                            )
                    extra_excludes: set[str] = set()
                    if scope == "project":
                        extra_excludes.add(str(op.get("_project_raw") or ""))
                        extra_excludes.add(str(op.get("_project_stem") or ""))
                    candidate_links, exclude_names = _collect_candidate_links(
                        config,
                        op_links=list(op.get("links") or []),
                        title=title,
                        content=content,
                        self_rel=rel,
                        self_stem=target.stem,
                        extra_excludes=extra_excludes,
                    )

                    if scope == "project":
                        proj_title = str(op.get("_project_raw") or op.get("_project_stem") or title)
                        if existing is None:
                            fm_proj: dict[str, Any] = {
                                "type": "project",
                                "title": proj_title,
                                "summary": op.get("summary") or _derive_note_summary(content),
                                "proactive_owned": True,
                                "created": today_str,
                                "updated": today_str,
                                "source": op["_trusted_source"],
                                "domain": op["_trusted_domain"],
                                "status": "active",
                                "evidence_level": op["evidence_level"],
                            }
                            if fact_key:
                                fm_proj["fact_key"] = fact_key
                            proj_keys = _merge_str_list_ordered([], _normalize_str_list(op.get("keys")))
                            if proj_keys:
                                fm_proj["keys"] = proj_keys
                            proj_tags = _merge_str_list_ordered([], _normalize_str_list(op.get("tags")))
                            if proj_tags:
                                fm_proj["tags"] = proj_tags
                            proj_refs = _merge_str_list_ordered([], op["source_refs"], limit=MAX_SOURCE_REFS)
                            if proj_refs:
                                fm_proj["source_refs"] = proj_refs
                            link_lines = _merge_link_lines([], candidate_links, exclude_names=exclude_names)
                            source_lines = _format_source_lines(session_sources)
                            managed_body = _render_managed_sections(
                                "",
                                [_ManagedSection(heading=title, body=content, section_key=op.get("section_key"))],
                                link_lines,
                                source_lines,
                            )
                            header = f"{_dump_frontmatter(fm_proj)}\n# {proj_title}\n\n"
                            rendered = _merge_managed_block(None, default_header=header, managed_body=managed_body)
                        else:
                            rendered = _format_longterm_note_content(
                                existing_text=existing,
                                note_type="project",
                                title=title,
                                content=content,
                                keys=_normalize_str_list(op.get("keys")),
                                tags=_normalize_str_list(op.get("tags")),
                                fact_key=fact_key,
                                evidence_level=op["evidence_level"],
                                source_refs=op["source_refs"],
                                trusted_source=op["_trusted_source"],
                                trusted_domain=op["_trusted_domain"],
                                today_str=today_str,
                                candidate_links=candidate_links,
                                exclude_link_names=exclude_names,
                                session_sources=session_sources,
                                as_section=True,
                                section_key=op.get("section_key"),
                                summary=op.get("summary"),
                            )
                        touched_project = True
                    else:
                        rendered = _format_longterm_note_content(
                            existing_text=existing,
                            note_type=note_type,
                            title=title,
                            content=content,
                            keys=_normalize_str_list(op.get("keys")),
                            tags=_normalize_str_list(op.get("tags")),
                            fact_key=fact_key,
                            evidence_level=op["evidence_level"],
                            source_refs=op["source_refs"],
                            trusted_source=op["_trusted_source"],
                            trusted_domain=op["_trusted_domain"],
                            today_str=today_str,
                            candidate_links=candidate_links,
                            exclude_link_names=exclude_names,
                            session_sources=session_sources,
                            section_key=op.get("section_key"),
                            summary=op.get("summary"),
                        )

                    if existing is not None and existing == rendered:
                        skipped_dedupe += 1
                        op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(target, rendered)
                    post_write_hashes[rel] = sha256_text(rendered)
                    touched_non_diary = True

                elif op_type == "project_update":
                    target = op["_resolved_target"]
                    existing = target.read_text(encoding="utf-8") if target.exists() else None
                    title = str(op.get("title") or op.get("_project_raw") or op["project"])
                    candidate_links, exclude_names = _collect_candidate_links(
                        config,
                        op_links=list(op.get("links") or []),
                        title=title,
                        content=op["managed_content"],
                        self_rel=rel,
                        self_stem=target.stem,
                        extra_excludes={str(op.get("project") or ""), str(op.get("_project_raw") or "")},
                    )
                    existing_managed = _extract_managed_block(existing) if existing is not None else None
                    _, existing_sections, existing_links = _parse_managed_sections(existing_managed)
                    link_lines = _merge_link_lines(existing_links, candidate_links, exclude_names=exclude_names)
                    managed_body = _render_managed_sections(op["managed_content"], existing_sections, link_lines)

                    explicit_summary = op.get("summary")
                    derived_summary = _derive_note_summary(op["managed_content"])
                    if existing is None:
                        fm_new: dict[str, Any] = {
                            "type": "project",
                            "title": title,
                            "summary": explicit_summary or derived_summary,
                            "proactive_owned": True,
                            "status": "active",
                        }
                        default_header = f"{_dump_frontmatter(fm_new)}\n# {title}\n\n"
                        merged = _merge_managed_block(
                            None,
                            default_header=default_header,
                            managed_body=managed_body,
                        )
                    else:
                        fm_cur, body_cur = _parse_frontmatter_and_body(existing)
                        fm_changed = False
                        if explicit_summary and str(fm_cur.get("summary") or "").strip() != explicit_summary:
                            fm_cur["summary"] = explicit_summary
                            fm_changed = True
                        elif not str(fm_cur.get("summary") or "").strip() and derived_summary:
                            fm_cur["summary"] = derived_summary
                            fm_changed = True
                        if fm_changed:
                            if not fm_cur.get("type"):
                                fm_cur["type"] = "project"
                            if not fm_cur.get("title"):
                                fm_cur["title"] = title
                            merged_body = _merge_managed_block(
                                body_cur.lstrip("\r\n"),
                                default_header=f"# {fm_cur.get('title') or title}\n\n",
                                managed_body=managed_body,
                            )
                            merged = f"{_dump_frontmatter(fm_cur)}\n{merged_body}"
                        else:
                            default_header = (
                                f"---\ntype: project\ntitle: {json.dumps(title, ensure_ascii=False)}\n"
                                f"proactive_owned: true\nstatus: active\n---\n\n# {title}\n\n"
                            )
                            merged = _merge_managed_block(
                                existing,
                                default_header=default_header,
                                managed_body=managed_body,
                            )
                    if existing is not None and existing == merged:
                        skipped_dedupe += 1
                        op_records.append({"op_id": op_id, "op_type": op_type, "status": "skipped_no_change"})
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
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
        except OSError as exc:
            if applied_count > 0:
                msg = (
                    f"批量写入中途发生 I/O 异常（部分已写：{applied_count} 条已记入 "
                    f"proactive_applied_ops）：{exc}"
                )
                raise OSError(msg) from exc
            raise

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
        diary_rate_limited_count=diary_rate_limited,
    )
