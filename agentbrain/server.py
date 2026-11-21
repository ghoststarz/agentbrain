"""HTTP 服务与核心业务服务层（基于标准库 http.server，全 JSON 响应）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentbrain.acl import AuthError, AuthIdentity, authenticate_bearer, display_client_id
from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig
from agentbrain.diary import DiaryGateError, append_diary, read_diary, today_diary_status
from agentbrain.handoff import read_handoff, upsert_handoff
from agentbrain.index import connect_db, reindex
from agentbrain.parse import normalize_trigger_order, parse_note
from agentbrain.proactive_redaction import redact_secrets
from agentbrain.render import _extract_section, get_current_revision, render_boot, render_delta
from agentbrain.retrieve import filter_hits_for_inject, search_notes

_logger = logging.getLogger(__name__)

_TYPE_DIR_MAP: dict[str, str] = {
    "fact": "20_LongTerm/Facts",
    "decision": "20_LongTerm/Decisions",
    "entity": "20_LongTerm/Entities",
    "project": "20_LongTerm/Projects",
    "moc": "20_LongTerm/MOC",
}

_SAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_TITLE_ID_ANCHOR_RE = re.compile(r"[A-Za-z]+[-_]?\d+(?:[-_]\d+)*")
_NOTE_API_ALLOWED_PREFIXES = ("20_longterm/", "10_working/")
_NOTE_API_MAX_CONTENT_CHARS = 20000


def _derive_fallback_summary(body: str, max_chars: int = 100) -> str:
    """从正文首句派生摘要（截断至 max_chars 字）。"""
    first = ""
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if line:
            first = line
            break
    if not first:
        first = " ".join(str(body or "").split()).strip()
    if not first:
        return ""
    m = re.search(r"[。！？!?]|(?<!\d)\.(?=\s|$)", first)
    if m and m.start() > 0:
        first = first[: m.end()].strip()
    first = " ".join(first.split())
    if len(first) <= max_chars:
        return first
    return first[: max(1, max_chars - 1)].rstrip() + "…"


def _parse_str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if x is not None and str(x).strip()]
    else:
        items = []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _extract_hard_anchor_keys(
    title: str,
    fact_key: str | None,
    aliases: list[str],
) -> list[str]:
    """未传 keys 时的确定性兜底：仅提取 fact_key 对象名、标题编号型标识符、显式 aliases。"""
    candidates: list[str] = []
    if fact_key:
        obj_name = str(fact_key).strip().split(".")[0].strip()
        if obj_name:
            candidates.append(obj_name)
    for match in _TITLE_ID_ANCHOR_RE.finditer(title):
        candidates.append(match.group(0))
    candidates.extend(aliases)

    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _sanitize_filename(name: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("-", name).strip().strip(".")
    return cleaned[:80] if cleaned else "note"


def _norm_text(value: str) -> str:
    """归一化文本用于镜像笔记比对（折叠所有空白）。"""
    return " ".join(str(value or "").split())


def _note_tags(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("tags")
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _find_upsert_target(
    target_dir: Path,
    safe_stem: str,
    previous_content: str,
    tags: list[str],
) -> Path | None:
    """在目标目录里定位「本次写入应该覆盖的那一条」笔记；找不到返回 None。

    优先用 frontmatter 的 `mirror_hash` 对上旧正文的哈希（编辑记忆时最准），
    其次用旧正文归一化后的前缀做包含匹配，最后才按标题/文件名兜底。
    调用方带 `hermes-mirror` 标签时只覆盖同样带该标签的笔记，绝不吞掉手写笔记。
    """
    if not target_dir.is_dir():
        return None
    mirror_only = "hermes-mirror" in tags
    prev_norm = _norm_text(previous_content)
    prev_hash = hashlib.sha256(prev_norm.encode("utf-8")).hexdigest() if prev_norm else ""
    prev_probe = prev_norm[:200]

    by_hash: Path | None = None
    by_content: Path | None = None
    by_title: Path | None = None
    for path in sorted(target_dir.glob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            meta = parse_note(path, text).frontmatter or {}
        except Exception:  # noqa: BLE001
            meta = {}
        if mirror_only and "hermes-mirror" not in _note_tags(meta):
            continue
        if prev_hash and str(meta.get("mirror_hash") or "").strip() == prev_hash:
            by_hash = by_hash or path
            continue
        if prev_probe and prev_probe in _norm_text(text):
            by_content = by_content or path
            continue
        if path.stem == safe_stem or str(meta.get("title") or "").strip() == safe_stem:
            by_title = by_title or path
    return by_hash or by_content or by_title


class ServiceError(Exception):
    """业务逻辑错误，携带 HTTP 状态码与 JSON 错误信息。"""

    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


class BrokerService:
    """共享的记忆 Broker 服务层，供 HTTP Server 与 MCP Server 复用。"""

    def __init__(self, config: AppConfig, tokens_path: Path | None = None) -> None:
        self.config = config
        self.tokens_path = tokens_path
        self._write_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._cooldowns: dict[tuple[str, str], dict[str, int]] = {}
        self._default_cooldown_turns: int = 5

    def _sync_agy_quiet(self) -> None:
        """写操作改变 revision 后，顺手把 agy 的全局规则文件刷到最新。

        只做「渲染 + 比对 + 变了才写」，2KB 级文件，代价可忽略。写失败一律吞掉：
        绝不能让刷新 agy 文件这件事拖死写入本身。
        """
        try:
            from agentbrain.agy_rules import sync_agy_rules

            sync_agy_rules(self.config)
        except Exception:  # noqa: BLE001 —— fail-open
            pass

    def diary_status(self, *, can_casual: bool = False) -> dict[str, Any]:
        """今日公账/私账的条数与作者（只元信息，不含正文）。

        fail-open：这里出任何问题都只让状态行消失，绝不能让检索整体失败。
        门控：`diary.gate` 打开且调用方不是 casual 时，私账连「有没有」都不暴露。
        """
        try:
            status: dict[str, Any] = {"public": today_diary_status(self.config, "public")}
            if not self.config.memory.diary_gate or can_casual:
                status["private"] = today_diary_status(self.config, "private")
            return status
        except Exception:  # noqa: BLE001 —— 状态行是锦上添花，坏了就退化
            return {}

    def healthz(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile": self.config.memory.profile,
            "revision": get_current_revision(self.config),
        }

    def authenticate(self, auth_header: str | None) -> AuthIdentity:
        return authenticate_bearer(auth_header, self.config, tokens_path=self.tokens_path)

    def boot(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = str(payload.get("target") or "codex")
        return render_boot(self.config, target=target)

    def delta(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            since_rev = int(payload.get("since_rev", 0))
        except (TypeError, ValueError) as exc:
            raise ServiceError(400, "invalid_request", f"since_rev must be integer: {exc}") from exc
        target = str(payload.get("target") or "codex")
        return render_delta(self.config, since_rev=since_rev, target=target)

    def _write_recall_audit(
        self,
        *,
        client_id: str,
        session_id: str,
        query: str,
        raw_hits: list[dict[str, Any]],
        final_hits: list[dict[str, Any]],
        revision: int,
    ) -> None:
        """向 audit_dir/recall.jsonl 追加单行召回审计记录（fail-open，绝不拖垮检索）。"""
        try:
            audit_dir = self.config.broker.audit_dir
            audit_dir.mkdir(parents=True, exist_ok=True)
            recall_path = audit_dir / "recall.jsonl"
            safe_query = redact_secrets(query)[:120]
            weak_count = sum(1 for h in raw_hits if bool(h.get("weak_evidence")))
            top_score = max((float(h.get("score") or 0.0) for h in final_hits), default=0.0)
            entry = {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "client": client_id,
                "session": session_id,
                "query": safe_query,
                "hits": len(final_hits),
                "top_score": round(top_score, 4),
                "weak": weak_count,
                "injected": bool(final_hits),
                "revision": revision,
            }
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            with recall_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("failed to write recall audit (ignored): %s", exc)

    def search(
        self,
        payload: dict[str, Any],
        *,
        client_id: str = "default",
        can_casual: bool = False,
    ) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ServiceError(400, "invalid_request", "query is required")
        k_val = payload.get("k")
        hops_val = payload.get("hops")
        k_override = int(k_val) if k_val is not None else None
        hops_override = int(hops_val) if hops_val is not None else None
        session_id = str(payload.get("session_id") or "").strip()
        inject_mode = bool(payload.get("inject", False))

        if not session_id:
            result = search_notes(
                self.config,
                query,
                k_override=k_override,
                hops_override=hops_override,
            )
            raw_hits = list(result.get("hits") or [])
            if inject_mode:
                result = filter_hits_for_inject(result, self.config.memory.proactive.prefetch_min_score)
            revision = get_current_revision(self.config)
            result["revision"] = revision
            self._write_recall_audit(
                client_id=client_id,
                session_id="",
                query=query,
                raw_hits=raw_hits,
                final_hits=list(result.get("hits") or []),
                revision=revision,
            )
            return result

        cd_key = (client_id, session_id)
        with self._cooldown_lock:
            session_cd = self._cooldowns.setdefault(cd_key, {})
            expired = [p for p, rem in session_cd.items() if rem - 1 <= 0]
            for p in expired:
                del session_cd[p]
            for p in list(session_cd):
                session_cd[p] -= 1
            blocked = set(session_cd.keys())

        result = search_notes(
            self.config,
            query,
            k_override=k_override,
            hops_override=hops_override,
            cooldown_blocked=blocked,
        )
        raw_hits = list(result.get("hits") or [])
        if inject_mode:
            result = filter_hits_for_inject(result, self.config.memory.proactive.prefetch_min_score)

        with self._cooldown_lock:
            session_cd = self._cooldowns.setdefault(cd_key, {})
            for hit in result.get("hits", []):
                hit_path = str(hit.get("path") or "")
                if hit_path:
                    session_cd[hit_path] = self._default_cooldown_turns

        revision = get_current_revision(self.config)
        result["revision"] = revision
        self._write_recall_audit(
            client_id=client_id,
            session_id=session_id,
            query=query,
            raw_hits=raw_hits,
            final_hits=list(result.get("hits") or []),
            revision=revision,
        )
        if self.config.memory.proactive.enabled:
            receipts = self._proactive_store().list_pending_receipts(
                client_id=client_id,
                session_id=session_id,
            )
            if receipts:
                result["review_receipts"] = receipts
        return result

    def read_note(
        self,
        payload: dict[str, Any],
        *,
        can_casual: bool = False,
    ) -> dict[str, Any]:
        note_ref = str(payload.get("note") or "").strip()
        if not note_ref:
            raise ServiceError(400, "invalid_request", "note is required")
        block = payload.get("block")
        block_name = str(block).strip() if block is not None and str(block).strip() else None

        profile_root = self.config.profile_dir.resolve()
        candidate = (profile_root / note_ref).resolve()

        # 防止路径穿越出 profile_dir
        if profile_root not in candidate.parents and candidate != profile_root:
            raise ServiceError(403, "forbidden", "path outside profile directory")

        if not candidate.exists() or not candidate.is_file():
            # 回退按标题或文件名在索引库中查找
            resolved_rel = self._lookup_note_relpath(note_ref)
            if resolved_rel is None:
                raise ServiceError(404, "not_found", f"note not found: {note_ref}")
            candidate = (profile_root / resolved_rel).resolve()
            if not candidate.exists():
                raise ServiceError(404, "not_found", f"note file missing: {resolved_rel}")

        rel_path = candidate.relative_to(profile_root).as_posix()
        if rel_path.lower().startswith("30_diary/private/") and self.config.memory.diary_gate and not can_casual:
            raise ServiceError(403, "forbidden", "private diary gate is enabled")

        raw = candidate.read_text(encoding="utf-8")
        parsed = parse_note(candidate, raw)
        content = raw
        if block_name:
            extracted = _extract_section(parsed.body, rf"^#{{1,6}}\s+.*{re.escape(block_name)}")
            content = extracted if extracted else ""

        return {
            "path": rel_path,
            "title": parsed.title,
            "block": block_name,
            "content": content,
        }

    def _lookup_note_relpath(self, note_ref: str) -> str | None:
        db_path = self.config.broker.db_path
        if not db_path.exists():
            return None
        stem = note_ref[:-3] if note_ref.lower().endswith(".md") else note_ref
        with connect_db(db_path) as conn:
            row = conn.execute(
                "SELECT path FROM notes WHERE path = ? OR title = ? LIMIT 1",
                (note_ref.replace("\\", "/"), stem),
            ).fetchone()
            return str(row["path"]) if row else None

    def _validate_note_api_path(self, rel_path: str) -> tuple[Path, str]:
        """校验 /v1/note 路径：必须落在 profile_dir 内的 20_LongTerm/ 或 10_Working/ 下，禁止穿越与受限目录。"""
        raw = str(rel_path or "").replace("\\", "/").strip()
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw or raw.startswith("/") or ":" in raw:
            raise ServiceError(403, "forbidden", f"path outside allowed directories: {rel_path}")
        parts = Path(raw).parts
        if any(p in ("..", ".", "") for p in parts):
            raise ServiceError(403, "forbidden", f"invalid path segment: {rel_path}")
        if any(p.startswith(".") for p in parts):
            raise ServiceError(403, "forbidden", f"hidden path is not allowed: {rel_path}")

        profile_root = self.config.profile_dir.resolve()
        cur = self.config.profile_dir
        for part in parts:
            cur = cur / part
            if cur.exists() and cur.is_symlink():
                raise ServiceError(403, "forbidden", f"symlink access denied: {rel_path}")

        candidate = (self.config.profile_dir / Path(*parts)).resolve()
        if not candidate.is_relative_to(profile_root):
            raise ServiceError(403, "forbidden", "path outside profile directory")

        canonical_rel = candidate.relative_to(profile_root).as_posix()
        lower_rel = canonical_rel.lower()
        if not any(lower_rel.startswith(prefix) for prefix in _NOTE_API_ALLOWED_PREFIXES):
            raise ServiceError(403, "forbidden", f"path not in 20_LongTerm/ or 10_Working/: {canonical_rel}")
        return candidate, canonical_rel

    def _resolve_note_by_title(self, title_ref: str) -> str | None:
        """在 20_LongTerm/ 与 10_Working/ 中按标题或 stem 查找笔记相对路径。"""
        stem = title_ref[:-3] if title_ref.lower().endswith(".md") else title_ref
        db_path = self.config.broker.db_path
        if db_path.exists():
            with connect_db(db_path) as conn:
                row = conn.execute(
                    "SELECT path FROM notes WHERE title = ? OR path = ? LIMIT 1",
                    (stem, title_ref.replace("\\", "/")),
                ).fetchone()
                if row:
                    return str(row["path"])
                all_rows = conn.execute("SELECT path, title, fm_json FROM notes").fetchall()
                for r in all_rows:
                    rel = str(r["path"])
                    if Path(rel).stem == stem:
                        return rel
                    try:
                        fm = json.loads(r["fm_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        fm = {}
                    if isinstance(fm, dict):
                        if str(fm.get("title") or "").strip() == stem:
                            return rel
                        aliases = fm.get("aliases") or []
                        if isinstance(aliases, list) and any(str(a).strip() == stem for a in aliases):
                            return rel

        # 索引库未命中或尚未建库时，回退扫描允许目录
        for subdir in ("20_LongTerm", "10_Working"):
            base = self.config.profile_dir / subdir
            if not base.exists() or base.is_symlink():
                continue
            for md in sorted(base.rglob("*.md")):
                if md.is_symlink():
                    continue
                rel_parts = md.relative_to(self.config.profile_dir).parts
                if any(p.startswith(".") for p in rel_parts):
                    continue
                if md.stem == stem or md.name == title_ref:
                    return "/".join(rel_parts)
                try:
                    parsed = parse_note(md, md.read_text(encoding="utf-8", errors="replace"))
                    if str(parsed.frontmatter.get("title") or "").strip() == stem:
                        return "/".join(rel_parts)
                except OSError:
                    continue
        return None

    def get_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/note：按 title 或 path 读取 20_LongTerm/ 与 10_Working/ 下的单篇笔记。"""
        raw_path = str(payload.get("path") or "").strip()
        raw_title = str(payload.get("title") or "").strip()
        if not raw_path and not raw_title:
            raise ServiceError(400, "invalid_request", "title or path is required")

        if raw_path:
            candidate, rel_path = self._validate_note_api_path(raw_path)
            if not candidate.exists() or not candidate.is_file():
                raise ServiceError(404, "not_found", f"note not found: {rel_path}")
        else:
            found_rel = self._resolve_note_by_title(raw_title)
            if found_rel is None:
                raise ServiceError(404, "not_found", f"note not found: {raw_title}")
            candidate, rel_path = self._validate_note_api_path(found_rel)
            if not candidate.exists() or not candidate.is_file():
                raise ServiceError(404, "not_found", f"note file missing: {rel_path}")

        raw = candidate.read_text(encoding="utf-8")
        parsed = parse_note(candidate, raw)
        fm = parsed.frontmatter
        summary = " ".join(str(fm.get("summary") or "").split()).strip()
        if not summary:
            summary = _derive_fallback_summary(parsed.body, max_chars=100)
        updated = str(fm.get("updated") or "").strip()

        seen_links: set[tuple[str, str]] = set()
        links: list[dict[str, str]] = []
        for lnk in parsed.links:
            key = (lnk.dst_title, lnk.relation)
            if key not in seen_links:
                seen_links.add(key)
                links.append({"title": lnk.dst_title, "relation": lnk.relation})

        truncated = len(raw) > _NOTE_API_MAX_CONTENT_CHARS
        content = raw[:_NOTE_API_MAX_CONTENT_CHARS] if truncated else raw

        result: dict[str, Any] = {
            "title": str(fm.get("title") or parsed.title),
            "path": rel_path,
            "summary": summary,
            "content": content,
            "links": links,
            "updated": updated,
        }
        if truncated:
            result["truncated"] = True
        return result

    def write_note(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ServiceError(400, "invalid_request", "content is required")

        note_type = str(payload.get("type") or "fact").strip().lower()
        links_raw = payload.get("links") or []
        if isinstance(links_raw, str):
            links = [x.strip() for x in links_raw.split(",") if x.strip()]
        elif isinstance(links_raw, list):
            links = [str(x).strip() for x in links_raw if str(x).strip()]
        else:
            links = []

        tags_raw = payload.get("tags") or []
        if isinstance(tags_raw, str):
            tags = [x.strip() for x in tags_raw.split(",") if x.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(x).strip() for x in tags_raw if str(x).strip()]
        else:
            tags = []

        supersedes = payload.get("supersedes")
        fact_key = payload.get("fact_key")
        title_raw = str(payload.get("title") or "").strip()
        aliases = _parse_str_list(payload.get("aliases"))
        keys = (
            _parse_str_list(payload.get("keys"))
            if "keys" in payload and payload.get("keys") is not None
            else _extract_hard_anchor_keys(
                title_raw or (content.splitlines()[0].lstrip("#").strip()[:60]),
                str(fact_key) if fact_key else None,
                aliases,
            )
        )
        keys2 = _parse_str_list(payload.get("keys2"))
        order = normalize_trigger_order(payload.get("order"), default=100)

        if not title_raw:
            first_line = content.splitlines()[0].lstrip("#").strip()
            title_raw = first_line[:60] if first_line else f"{note_type}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        target_subdir = _TYPE_DIR_MAP.get(note_type)
        if target_subdir is None:
            allowed = ", ".join(sorted(_TYPE_DIR_MAP))
            raise ServiceError(400, "invalid_request", f"unsupported note type: {note_type!r} (allowed: {allowed})")

        today_str = dt.date.today().isoformat()
        target_dir = self.config.profile_dir / target_subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_stem = _sanitize_filename(title_raw)
        upsert = bool(payload.get("upsert"))
        previous_content = str(payload.get("previous_content") or "").strip()

        created_str = today_str
        existing_target: Path | None = None
        if upsert:
            existing_target = _find_upsert_target(target_dir, safe_stem, previous_content, tags)
        if existing_target is not None:
            # 镜像/更新语义：原地覆盖，保留原文件路径与 created，不再另存带时间戳的副本
            target_file = existing_target
            try:
                prev_meta = parse_note(
                    existing_target,
                    existing_target.read_text(encoding="utf-8", errors="replace"),
                ).frontmatter or {}
            except Exception:  # noqa: BLE001
                prev_meta = {}
            created_str = str(prev_meta.get("created") or "").strip() or today_str
            prev_title = str(prev_meta.get("title") or "").strip()
            if prev_title:
                title_raw = prev_title
        else:
            target_file = target_dir / f"{safe_stem}.md"
            if target_file.exists():
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                target_file = target_dir / f"{safe_stem}-{stamp}.md"

        summary_raw = " ".join(str(payload.get("summary") or "").split()).strip()
        if summary_raw:
            summary_val = summary_raw[:120] if len(summary_raw) <= 120 else (summary_raw[:119].rstrip() + "…")
        else:
            summary_val = _derive_fallback_summary(content, max_chars=100)

        fm_lines = [
            "---",
            f"type: {note_type}",
            f"summary: {json.dumps(summary_val, ensure_ascii=False)}",
            f"created: {created_str}",
            f"updated: {today_str}",
            f"source: {identity.display_source}",
            f"domain: {identity.domain}",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
            "status: active",
            f"supersedes: {json.dumps(supersedes, ensure_ascii=False) if supersedes else 'null'}",
            f"fact_key: {json.dumps(fact_key, ensure_ascii=False) if fact_key else 'null'}",
            f"keys: {json.dumps(keys, ensure_ascii=False)}",
            f"keys2: {json.dumps(keys2, ensure_ascii=False)}",
            f"order: {order}",
        ]
        if "hermes-mirror" in tags:
            # 记录本次正文的指纹：下一次编辑记忆时据此精确命中同一条，不必靠标题猜
            fm_lines.append(f"mirror_hash: {hashlib.sha256(_norm_text(content).encode('utf-8')).hexdigest()}")
        if aliases:
            fm_lines.append(f"aliases: {json.dumps(aliases, ensure_ascii=False)}")
        fm_lines.append("---")
        body_lines = [f"# {title_raw}", "", content]
        if links:
            body_lines.append("")
            body_lines.append("## 关联")
            for link in links:
                clean_link = link.strip("[]")
                body_lines.append(f"- related_to [[{clean_link}]]")

        full_text = "\n".join(fm_lines + body_lines) + "\n"

        with self._write_lock:
            atomic_write_text(target_file, full_text)
            stats = reindex(self.config)

        self._sync_agy_quiet()

        rel_path = target_file.relative_to(self.config.profile_dir).as_posix()
        parsed = parse_note(target_file, full_text)
        has_links = bool(parsed.links)

        suggested_links: list[str] = []
        if not has_links:
            search_res = search_notes(self.config, title_raw + " " + content[:80], k_override=4)
            for hit in search_res.get("hits", []):
                hit_title = str(hit.get("title") or "")
                if hit_title and hit_title != title_raw and hit_title not in suggested_links:
                    suggested_links.append(hit_title)
                if len(suggested_links) >= 3:
                    break

        return {
            "ok": True,
            "path": rel_path,
            "title": title_raw,
            "revision": stats.revision,
            "orphan_warning": not has_links,
            "suggested_links": suggested_links,
        }

    def handoff(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "read").strip().lower()
        if action == "read":
            return read_handoff(self.config)
        if action == "upsert":
            try:
                with self._write_lock:
                    result = upsert_handoff(
                        self.config,
                        task_key=str(payload.get("task_key") or ""),
                        status=str(payload.get("status") or "open"),
                        summary=str(payload.get("summary") or ""),
                        next_steps=payload.get("next_steps"),
                        touched_files=payload.get("touched_files"),
                        updated_by=identity.display_source,
                    )
                self._sync_agy_quiet()
                return result
            except ValueError as exc:
                raise ServiceError(400, "invalid_request", str(exc)) from exc
        raise ServiceError(400, "invalid_request", f"unsupported handoff action: {action}")

    def diary(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "read").strip().lower()
        ledger = str(payload.get("ledger") or "public").strip().lower()
        try:
            if action == "append":
                with self._write_lock:
                    result = append_diary(
                        self.config,
                        ledger=ledger,
                        content=str(payload.get("content") or ""),
                        author=identity.display_source,
                        can_casual=identity.can_casual,
                    )
                # 日记正文不进检索、不进 boot，但「今日日记」那一行状态会写进
                # agy 的规则文件，所以写完要刷一次，否则另一端看到的条数是过期的。
                self._sync_agy_quiet()
                return result
            if action == "read":
                recent_days = int(payload.get("recent_days", 3))
                return read_diary(
                    self.config,
                    ledger=ledger,
                    recent_days=recent_days,
                    can_casual=identity.can_casual,
                )
        except DiaryGateError as exc:
            raise ServiceError(403, "forbidden", str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise ServiceError(400, "invalid_request", str(exc)) from exc
        raise ServiceError(400, "invalid_request", f"unsupported diary action: {action}")

    def status(self) -> dict[str, Any]:
        db_path = self.config.broker.db_path
        notes_cnt = chunks_cnt = links_cnt = revision = 0
        updated_at: str | None = None
        index_age_seconds: int | None = None

        if db_path.exists():
            with connect_db(db_path) as conn:
                notes_cnt = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
                chunks_cnt = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                links_cnt = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
                rev_row = conn.execute(
                    "SELECT revision, updated_at FROM rev WHERE scope = ?",
                    (self.config.memory.profile,),
                ).fetchone()
                if rev_row:
                    revision = int(rev_row["revision"])
                    updated_at = str(rev_row["updated_at"])
                    try:
                        ts = dt.datetime.fromisoformat(updated_at)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt.timezone.utc)
                        index_age_seconds = max(
                            0,
                            int((dt.datetime.now(dt.timezone.utc) - ts).total_seconds()),
                        )
                    except ValueError:
                        pass

        return {
            "ok": True,
            "profile": self.config.memory.profile,
            "notes": notes_cnt,
            "chunks": chunks_cnt,
            "links": links_cnt,
            "revision": revision,
            "updated_at": updated_at,
            "index_age_seconds": index_age_seconds,
        }

    def _proactive_store(self):
        from agentbrain.proactive import ProactiveStore

        return ProactiveStore.from_app_config(self.config)

    def review_events(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        """写入一条或多条主动评审事件（凭证脱敏 + 超限显式 413 拒绝）。"""
        if not self.config.memory.proactive.enabled:
            raise ServiceError(403, "proactive_disabled", "proactive review is disabled")
        from agentbrain.proactive import EVENT_KIND_HUMAN_TURN, PayloadTooLargeError
        from agentbrain.proactive_adapters import _parse_utc_iso

        raw_events = payload.get("events")
        if raw_events is None:
            raw_events = [payload]
        if not isinstance(raw_events, list) or not raw_events:
            raise ServiceError(400, "invalid_request", "events must be a non-empty list or single event object")

        store = self._proactive_store()
        results: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_events):
            if not isinstance(item, dict):
                raise ServiceError(400, "invalid_request", f"events[{idx}] must be an object")
            client = display_client_id(str(item.get("client") or identity.client_id or "").strip())
            session_id = str(item.get("session_id") or "").strip()
            event_id = str(item.get("event_id") or "").strip()
            event_kind = str(item.get("event_kind") or EVENT_KIND_HUMAN_TURN).strip()
            content = str(item.get("content") or "")
            qualifies_nightly = bool(item.get("qualifies_nightly", True))
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else None
            occ_raw = item.get("occurred_at")
            occ_dt = _parse_utc_iso(occ_raw) if occ_raw else None
            if occ_raw and occ_dt is None:
                raise ServiceError(400, "invalid_request", f"events[{idx}].occurred_at must be ISO-8601")

            try:
                res = store.ingest_event(
                    client=client,
                    session_id=session_id,
                    event_id=event_id,
                    event_kind=event_kind,
                    content=content,
                    occurred_at=occ_dt,
                    meta=meta,
                    qualifies_nightly=qualifies_nightly,
                    route="provider",
                )
            except PayloadTooLargeError as exc:
                raise ServiceError(413, "payload_too_large", str(exc)) from exc
            except ValueError as exc:
                raise ServiceError(400, "invalid_request", str(exc)) from exc

            results.append(
                {
                    "accepted": res.accepted,
                    "deduped": res.deduped,
                    "excluded": res.excluded,
                    "event_row_id": res.event_row_id,
                    "turn_seq": res.turn_seq,
                    "completed_human_turns": res.completed_human_turns,
                    "created_job_id": res.created_job_id,
                }
            )

        return {"ok": True, "count": len(results), "results": results}

    def review_cli_event(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        """参考用 CLI 引用事件接口（仅作证据，绝不计入人类完成轮次）。"""
        if not self.config.memory.proactive.enabled:
            raise ServiceError(403, "proactive_disabled", "proactive review is disabled")
        from agentbrain.proactive import PayloadTooLargeError
        from agentbrain.proactive_adapters import ingest_cli_reference_event

        session_id = str(payload.get("session_id") or identity.client_id or "cli").strip()
        content = str(payload.get("content") or "")
        event_id = str(payload.get("event_id") or "").strip() or None
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else None
        store = self._proactive_store()
        try:
            res = ingest_cli_reference_event(
                store,
                session_id=session_id,
                content=content,
                event_id=event_id,
                meta=meta,
            )
        except PayloadTooLargeError as exc:
            raise ServiceError(413, "payload_too_large", str(exc)) from exc
        except ValueError as exc:
            raise ServiceError(400, "invalid_request", str(exc)) from exc
        return {"ok": True, **res}

    def review_status(self) -> dict[str, Any]:
        """返回脱敏主动评审调度状态（仅元信息，绝不含任何聊天原文）。"""
        store = self._proactive_store()
        status = store.get_status()
        status["adapters"] = store.list_adapter_cursors()
        runner_st = store.get_adapter_cursor("proactive_runner", "status")
        status["runner"] = runner_st or {
            "last_error": None,
            "unavailable_sources": [],
        }
        status["enabled"] = bool(self.config.memory.proactive.enabled)
        status["ok"] = True
        return status

    def review_tick(self, payload: dict[str, Any]) -> dict[str, Any]:
        """手动触发一次夜间窗口调度检查与可选的 agy IDE 扫描。"""
        if not self.config.memory.proactive.enabled:
            return {"ok": True, "enabled": False, "skipped": True}
        from agentbrain.proactive_adapters import (
            resolve_hermes_statedb_path,
            scan_agy_ide_roots,
            scan_hermes_statedb,
        )

        store = self._proactive_store()
        ide_report = None
        hermes_report = None
        catchup = bool(payload.get("catchup", False))
        if (
            self.config.memory.proactive.agy_ide_enabled
            and self.config.memory.proactive.agy_ide_roots
        ):
            ide_report = scan_agy_ide_roots(
                store,
                self.config.memory.proactive.agy_ide_roots,
                catchup=catchup,
            )
        hermes_db_path = resolve_hermes_statedb_path(self.config)
        if hermes_db_path is not None:
            hermes_report = scan_hermes_statedb(store, hermes_db_path, catchup=catchup)
        tick = store.tick(include_historical=catchup)
        return {
            "ok": True,
            "tick": {
                "windows_checked": tick.windows_checked,
                "windows_skipped_empty": tick.windows_skipped_empty,
                "jobs_created": tick.jobs_created,
                "windows_reopened": tick.windows_reopened,
            },
            "ide": (
                {
                    "roots_scanned": ide_report.roots_scanned,
                    "transcripts_scanned": ide_report.transcripts_scanned,
                    "events_ingested": ide_report.events_ingested,
                    "turns_completed": ide_report.turns_completed,
                    "unavailable_sources": ide_report.unavailable_sources,
                }
                if ide_report is not None
                else None
            ),
            "hermes_statedb": (
                {
                    "available": hermes_report.available,
                    "sessions_scanned": hermes_report.sessions_scanned,
                    "turns_completed": hermes_report.turns_completed,
                    "events_ingested": hermes_report.events_ingested,
                    "events_deduped": hermes_report.events_deduped,
                    "skipped_pre_activation": hermes_report.skipped_pre_activation,
                    "skipped_excluded": hermes_report.skipped_excluded,
                    "last_error": hermes_report.last_error,
                }
                if hermes_report is not None
                else None
            ),
        }

    def review_receipts_ack(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        """确认已送达的后台评审完成回执。"""
        if not self.config.memory.proactive.enabled:
            return {"ok": True, "acked": 0}
        session_id = str(payload.get("session_id") or "").strip()
        raw_ids = payload.get("receipt_ids")
        if not session_id or not isinstance(raw_ids, list):
            raise ServiceError(400, "invalid_request", "session_id and receipt_ids list are required")
        receipt_ids = [int(rid) for rid in raw_ids if isinstance(rid, int) or (isinstance(rid, str) and rid.isdigit())]
        acked = self._proactive_store().ack_receipts(
            receipt_ids,
            client_id=identity.client_id,
            session_id=session_id,
        )
        return {"ok": True, "acked": acked}


class _BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: BrokerService,
    ) -> None:
        self.service = service
        super().__init__(server_address, handler_cls)


class BrokerHTTPHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：所有错误与响应一律返回 UTF-8 JSON。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """覆盖标准库默认的 HTML 错误页，确保任何 HTTP 错误都返回 JSON。"""
        self._send_json(code, {"error": message or "http_error", "detail": explain or ""})

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ServiceError(400, "invalid_request", "invalid Content-Length") from exc
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ServiceError(413, "payload_too_large", "request body exceeds 2MB")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "invalid_json", f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ServiceError(400, "invalid_json", "JSON body must be an object")
        return data

    def _handle_request(self, method: str) -> None:
        svc: BrokerService = self.server.service  # type: ignore[attr-defined]
        path = urlparse(self.path).path

        try:
            if method == "GET" and path == "/healthz":
                self._send_json(200, svc.healthz())
                return

            identity = svc.authenticate(self.headers.get("Authorization"))

            if method == "GET" and path == "/v1/status":
                self._send_json(200, svc.status())
                return
            if method == "GET" and path == "/v1/review/status":
                self._send_json(200, svc.review_status())
                return

            if method == "POST":
                payload = self._read_json_body()
                if path == "/v1/boot":
                    self._send_json(200, svc.boot(payload))
                    return
                if path == "/v1/delta":
                    self._send_json(200, svc.delta(payload))
                    return
                if path == "/v1/search":
                    self._send_json(200, svc.search(payload, client_id=identity.client_id, can_casual=identity.can_casual))
                    return
                if path == "/v1/read":
                    self._send_json(200, svc.read_note(payload, can_casual=identity.can_casual))
                    return
                if path == "/v1/note":
                    self._send_json(200, svc.get_note(payload))
                    return
                if path == "/v1/write":
                    self._send_json(200, svc.write_note(payload, identity=identity))
                    return
                if path == "/v1/handoff":
                    self._send_json(200, svc.handoff(payload, identity=identity))
                    return
                if path == "/v1/diary":
                    self._send_json(200, svc.diary(payload, identity=identity))
                    return
                if path == "/v1/review/events":
                    self._send_json(200, svc.review_events(payload, identity=identity))
                    return
                if path == "/v1/review/cli-event":
                    self._send_json(200, svc.review_cli_event(payload, identity=identity))
                    return
                if path == "/v1/review/tick":
                    self._send_json(200, svc.review_tick(payload))
                    return
                if path == "/v1/review/receipts/ack":
                    self._send_json(200, svc.review_receipts_ack(payload, identity=identity))
                    return

            self._send_json(404, {"error": "not_found", "path": path})
        except AuthError as exc:
            self._send_json(exc.status_code, {"error": exc.error, "detail": exc.detail})
        except ServiceError as exc:
            self._send_json(exc.status_code, {"error": exc.error, "detail": exc.detail})
        except Exception as exc:
            self._send_json(500, {"error": "internal_error", "detail": str(exc)})

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")


def create_http_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    tokens_path: Path | None = None,
) -> _BrokerHTTPServer:
    """创建已绑定端口的 HTTP 服务实例（端口占用时抛出明确 OSError）。"""
    bind_port = port if port is not None else config.broker.port
    service = BrokerService(config, tokens_path=tokens_path)
    return _BrokerHTTPServer((host, bind_port), BrokerHTTPHandler, service)


def run_http_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    tokens_path: Path | None = None,
) -> int:
    """启动 HTTP 服务并支持 Ctrl+C 干净退出；端口占用时打印清晰错误。"""
    bind_port = port if port is not None else config.broker.port
    try:
        httpd = create_http_server(config, host=host, port=bind_port, tokens_path=tokens_path)
    except OSError as exc:
        print(f"ERROR: 无法绑定 {host}:{bind_port}（可能端口已被占用）: {exc}")
        return 1

    try:
        from agentbrain.agy_rules import sync_agy_rules

        _agy = sync_agy_rules(config)
        if _agy.get("changed"):
            print(f"agy 规则文件已刷新：{_agy['path']} ({_agy['chars']} 字)")
    except Exception as _exc:  # 绝不能因为写规则文件失败而拖死服务
        print(f"WARN: agy 规则同步失败（忽略）：{_exc}")

    from agentbrain.proactive_runner import ProactiveRunner

    runner = ProactiveRunner(config, write_lock=httpd.service._write_lock)
    runner_started = runner.start()
    if runner_started:
        print("AgentBrain proactive background runner started")
    print(f"AgentBrain HTTP server listening on http://{host}:{bind_port} (profile={config.memory.profile})")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nShutting down AgentBrain HTTP server...")
    finally:
        runner.stop()
        httpd.shutdown()
        httpd.server_close()
    return 0
