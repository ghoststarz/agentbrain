"""证据打分、1 跳图谱扩散与预算裁剪检索模块。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import sqlite3
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from agentbrain.config import AppConfig, RetrievalConfig
from agentbrain.index import _ensure_notes_columns, connect_db
from agentbrain.segment import segment_query, segment_text

_logger = logging.getLogger(__name__)

_IDENTIFIER_SPAN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")


@dataclass(frozen=True)
class CandidateHit:
    """召回阶段产出的候选命中条目。"""

    note_id: int
    path: str
    title: str
    note_type: str
    score: float
    excerpt: str
    source: str
    trigger_order: int = 100
    weak_evidence: bool = False


def _parse_date(val: str | None) -> dt.date | None:
    """解析 YYYY-MM-DD 或 ISO 日期字符串。"""
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _compute_token_idf(
    conn: sqlite3.Connection,
    token: str,
    total_notes: int,
) -> float:
    """计算单个有效检索词的归一化 IDF 稀有度。"""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT c.note_id) AS df
        FROM chunks_fts f
        JOIN chunks c ON c.id = f.rowid
        WHERE chunks_fts MATCH ?
        """,
        (f'"{token}"',),
    ).fetchone()
    df = int(row["df"]) if row else 0
    if df <= 0:
        return 0.0
    denom = max(1.0, math.log(total_notes + 1.0))
    return math.log((total_notes + 1.0) / (df + 0.5)) / denom


def _compute_activity(
    category: str | None,
    last_activated: str | None,
    created: str | None,
    updated: str | None,
    confidence: float,
    pinned: int,
    half_life_days: float,
    today: dt.date,
) -> float:
    """计算记忆活性 A；只有 stable_profile 不衰减。"""
    if category == "stable_profile":
        return 1.0

    anchor = _parse_date(last_activated) or _parse_date(created) or _parse_date(updated)
    if anchor is None:
        return 1.0

    days = max(0, (today - anchor).days)
    importance = 1.0 if pinned else max(0.0, min(1.0, confidence)) * 0.5
    effective_half_life = max(1.0, half_life_days * (1.0 + 2.0 * importance))
    return float(2.0 ** (-days / effective_half_life))


def _compute_validity(valid_to: str | None, today: dt.date) -> float:
    """计算有效期 V：valid_to 为空或未过期为 1.0，已过期降权为 0.0。"""
    end_date = _parse_date(valid_to)
    if end_date is None:
        return 1.0
    return 1.0 if end_date >= today else 0.0


def _strip_leading_non_body(text: str, title: str | None = None) -> str:
    """跳过 frontmatter 残渣、前导笔记标题与 Markdown 标题行，提取正文内容。"""
    lines = text.splitlines()
    idx = 0
    if lines and lines[0].strip() == "---":
        idx = 1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                idx = i + 1
                break

    body_lines: list[str] = []
    norm_title = title.strip() if title else None
    for line in lines[idx:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if not body_lines and norm_title and stripped == norm_title:
            continue
        body_lines.append(stripped)

    if body_lines:
        return "\n".join(body_lines)

    compact = " ".join(line.strip() for line in lines if line.strip())
    compact = re.sub(r"^#+\s*", "", compact)
    return compact


def _make_excerpt(text: str, max_len: int = 240, *, title: str | None = None) -> str:
    """按句子边界生成命中摘要片段（目标 160–240 字，优先句末标点与换行）。"""
    body = _strip_leading_non_body(text, title=title)
    if not body:
        return ""

    compacted = " ".join(body.split())
    min_sentence_len = min(80, max_len // 2)
    min_clause_len = min(40, max_len // 3)
    sentence_re = r"[。！？!?]|(?<!\b\d)(?<!\.)\.(?=\s|$)"

    if len(compacted) <= max_len:
        if compacted[-1] not in "。！？.!?…":
            tail_matches = list(re.finditer(sentence_re, compacted))
            if tail_matches and tail_matches[-1].end() >= min_sentence_len:
                return compacted[: tail_matches[-1].end()]
        return compacted

    window = body[:max_len]
    sentence_matches = list(re.finditer(sentence_re, window))

    if sentence_matches and sentence_matches[-1].end() >= min_sentence_len:
        return " ".join(window[: sentence_matches[-1].end()].split())

    nl_idx = window.rfind("\n")
    if nl_idx >= min_clause_len:
        candidate = " ".join(window[:nl_idx].split())
        if candidate:
            if candidate[-1] not in "。！？.!?…":
                candidate = candidate.rstrip("，,；;：: ") + "…"
            return candidate

    semi_matches = list(re.finditer(r"[；;]", window))
    if semi_matches and semi_matches[-1].start() >= min_clause_len:
        candidate = " ".join(window[: semi_matches[-1].start()].split()).rstrip("，, ")
        if candidate:
            return candidate + "…"

    if sentence_matches:
        return " ".join(window[: sentence_matches[-1].end()].split())

    hard_cut = " ".join(window[: max(1, max_len - 1)].split())
    return hard_cut + "…"


def _fetch_connected(
    conn: sqlite3.Connection,
    note_id: int,
    limit: int = 2,
) -> list[dict[str, str]]:
    """获取 1 跳关联节点与 context_line（出边 + 入边，优先类型化关系）。"""
    candidates: list[tuple[int, dict[str, str]]] = []
    seen: set[tuple[str, str]] = set()

    out_rows = conn.execute(
        """
        SELECT dst_title, relation, context_line
        FROM links
        WHERE src_id = ?
        """,
        (note_id,),
    ).fetchall()
    for r in out_rows:
        item = {
            "title": str(r["dst_title"]),
            "relation": str(r["relation"]),
            "context_line": str(r["context_line"]),
        }
        key = (item["title"], item["relation"])
        if key not in seen:
            seen.add(key)
            priority = 0 if item["relation"] != "links_to" else 1
            candidates.append((priority, item))

    in_rows = conn.execute(
        """
        SELECT n.title AS src_title, l.relation, l.context_line
        FROM links l
        JOIN notes n ON n.id = l.src_id
        WHERE l.dst_id = ?
        """,
        (note_id,),
    ).fetchall()
    for r in in_rows:
        item = {
            "title": str(r["src_title"]),
            "relation": str(r["relation"]),
            "context_line": str(r["context_line"]),
        }
        key = (item["title"], item["relation"])
        if key not in seen:
            seen.add(key)
            priority = 0 if item["relation"] != "links_to" else 2
            candidates.append((priority, item))

    candidates.sort(key=lambda pair: pair[0])
    return [item for _, item in candidates[:limit]]


def _key_matches_query(key: str, query: str, query_lower: str) -> bool:
    """Trigger 键匹配：ASCII 键按词边界且忽略大小写，中文/非 ASCII 键用子串匹配。"""
    cleaned = key.strip()
    if not cleaned:
        return False
    if cleaned.isascii():
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(cleaned)}(?![A-Za-z0-9_])"
        return bool(re.search(pattern, query, re.IGNORECASE))
    return cleaned.lower() in query_lower


def _extract_title_id_tokens(title: str) -> set[str]:
    """提取标题中编号型标识符（如 ADR-03、v2、纯数字串）的分词 token 集合。"""
    id_tokens: set[str] = set()
    for span in _IDENTIFIER_SPAN_RE.findall(title):
        if any(ch.isdigit() for ch in span):
            id_tokens.update(segment_text(span))
    return id_tokens


def _token_title_weight(
    token: str,
    title_tokens: AbstractSet[str],
    title_id_tokens: AbstractSet[str],
) -> float:
    """计算查询 token 的标题与标识符加权系数。"""
    if token not in title_tokens:
        return 1.0
    weight = 2.5
    if token in title_id_tokens:
        weight *= 1.5
    return weight


def recall_trigger(
    conn: sqlite3.Connection,
    query: str,
    *,
    cooldown_blocked: AbstractSet[str] = frozenset(),
) -> list[CandidateHit]:
    """Trigger 精确触发召回：支持 keys 任一命中、keys2 消歧及 order 升序排序。"""
    if not query or not query.strip():
        return []

    query_lower = query.lower()
    rows = conn.execute(
        """
        SELECT id, path, title, type, keys_json, keys2_json, trigger_order
        FROM notes
        WHERE keys_json IS NOT NULL AND keys_json != '[]'
        """
    ).fetchall()

    hits: list[CandidateHit] = []
    for row in rows:
        path = str(row["path"])
        if path in cooldown_blocked:
            continue

        try:
            keys = json.loads(row["keys_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            keys = []
        if not isinstance(keys, list) or not keys:
            continue

        if not any(_key_matches_query(str(k), query, query_lower) for k in keys):
            continue

        try:
            keys2 = json.loads(row["keys2_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            keys2 = []
        if isinstance(keys2, list) and keys2:
            if not any(_key_matches_query(str(k2), query, query_lower) for k2 in keys2):
                continue

        nid = int(row["id"])
        title = str(row["title"])
        chunk_row = conn.execute(
            "SELECT text FROM chunks WHERE note_id = ? ORDER BY idx ASC LIMIT 1",
            (nid,),
        ).fetchone()
        excerpt = _make_excerpt(str(chunk_row["text"]), title=title) if chunk_row else title
        order = int(row["trigger_order"]) if row["trigger_order"] is not None else 100
        clamped_order = max(0, min(order, 200))
        score = round(1.2 + 0.6 * (1.0 - clamped_order / 200.0), 4)

        hits.append(
            CandidateHit(
                note_id=nid,
                path=path,
                title=str(row["title"]),
                note_type=row["type"] or "note",
                score=score,
                excerpt=excerpt,
                source="trigger",
                trigger_order=order,
                weak_evidence=False,
            )
        )

    hits.sort(key=lambda h: (h.trigger_order, h.path))
    return hits


def recall_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    ret_cfg: RetrievalConfig,
    today: dt.date | None = None,
) -> tuple[list[CandidateHit], bool]:
    """FTS5 二元组召回：含 IDF、标题/标识符加权、活性衰减与弱证据回退。"""
    fts_expr, _all_tokens, evidence_tokens = segment_query(query)
    if not fts_expr or not evidence_tokens:
        return [], False

    total_notes = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
    if total_notes == 0:
        return [], False

    ref_date = today or dt.date.today()
    token_idfs: dict[str, float] = {
        tok: _compute_token_idf(conn, tok, total_notes) for tok in evidence_tokens
    }

    matched_rows = conn.execute(
        """
        SELECT c.id AS chunk_id, c.note_id, c.text, c.seg, bm25(chunks_fts) AS bm25_rank
        FROM chunks_fts f
        JOIN chunks c ON c.id = f.rowid
        WHERE chunks_fts MATCH ?
        ORDER BY bm25(chunks_fts) ASC
        """,
        (fts_expr,),
    ).fetchall()

    if not matched_rows:
        return [], False

    note_rows_cache: dict[int, sqlite3.Row | None] = {}
    note_title_tokens_cache: dict[int, tuple[set[str], set[str]]] = {}

    def _get_note_and_title_tokens(nid: int) -> tuple[sqlite3.Row | None, set[str], set[str]]:
        if nid not in note_rows_cache:
            nrow = conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
            note_rows_cache[nid] = nrow
            if nrow is not None:
                title = str(nrow["title"])
                note_title_tokens_cache[nid] = (
                    set(segment_text(title)),
                    _extract_title_id_tokens(title),
                )
            else:
                note_title_tokens_cache[nid] = (set(), set())
        t_toks, t_id_toks = note_title_tokens_cache[nid]
        return note_rows_cache[nid], t_toks, t_id_toks

    note_best_chunk: dict[int, tuple[float, str]] = {}
    note_best_bm25: dict[int, tuple[float, str]] = {}
    note_matched_tokens: dict[int, set[str]] = {}

    for row in matched_rows:
        nid = int(row["note_id"])
        nrow, title_tokens, title_id_tokens = _get_note_and_title_tokens(nid)
        if nrow is None:
            continue

        seg_tokens = set(str(row["seg"]).split())
        hit_tokens = {t for t in evidence_tokens if t in seg_tokens}
        chunk_c = sum(
            token_idfs.get(t, 0.0) * _token_title_weight(t, title_tokens, title_id_tokens)
            for t in hit_tokens
        )

        note_matched_tokens.setdefault(nid, set()).update(hit_tokens)
        prev = note_best_chunk.get(nid)
        if prev is None or chunk_c > prev[0]:
            note_best_chunk[nid] = (chunk_c, str(row["text"]))

        bm25_rank = float(row["bm25_rank"])
        prev_bm25 = note_best_bm25.get(nid)
        if prev_bm25 is None or bm25_rank < prev_bm25[0]:
            note_best_bm25[nid] = (bm25_rank, str(row["text"]))

    scored_hits: list[CandidateHit] = []
    for nid, matched_toks in note_matched_tokens.items():
        note_row, title_tokens, title_id_tokens = _get_note_and_title_tokens(nid)
        if note_row is None:
            continue

        raw_evidence_c = sum(token_idfs.get(t, 0.0) for t in matched_toks)
        if raw_evidence_c < ret_cfg.min_evidence:
            continue

        evidence_c = sum(
            token_idfs.get(t, 0.0) * _token_title_weight(t, title_tokens, title_id_tokens)
            for t in matched_toks
        )

        fm = json.loads(note_row["fm_json"])
        activity_a = _compute_activity(
            category=note_row["category"],
            last_activated=note_row["last_activated"],
            created=note_row["created"],
            updated=note_row["updated"],
            confidence=float(note_row["confidence"] or 1.0),
            pinned=int(note_row["pinned"] or 0),
            half_life_days=ret_cfg.half_life_days,
            today=ref_date,
        )
        validity_v = _compute_validity(note_row["valid_to"], ref_date)

        in_deg_row = conn.execute(
            "SELECT COUNT(*) FROM links WHERE dst_id = ?",
            (nid,),
        ).fetchone()
        in_degree = int(in_deg_row[0]) if in_deg_row else 0
        hub_h = min(1.0, in_degree / 10.0)

        base_score = (evidence_c * activity_a) + 0.30 * hub_h + 0.10 * validity_v
        superseded_by = fm.get("superseded_by")
        final_score = base_score * ret_cfg.superseded_penalty if superseded_by else base_score

        scored_hits.append(
            CandidateHit(
                note_id=nid,
                path=str(note_row["path"]),
                title=str(note_row["title"]),
                note_type=note_row["type"] or "note",
                score=round(final_score, 4),
                excerpt=_make_excerpt(note_best_chunk[nid][1], title=str(note_row["title"])),
                source="fts",
                weak_evidence=False,
            )
        )

    evidence_bypassed = False
    if not scored_hits and any(len(t) >= 2 for t in evidence_tokens):
        evidence_bypassed = True
        bm25_candidates = sorted(note_best_bm25.items(), key=lambda item: item[1][0])[: ret_cfg.seed_k]
        raw_scores = [max(0.0, -item[1][0]) for item in bm25_candidates]
        max_raw = max(raw_scores) if raw_scores else 0.0
        for idx, (nid, (bm25_rank, best_text)) in enumerate(bm25_candidates):
            note_row = note_rows_cache.get(nid)
            if note_row is None:
                continue
            raw_bm25 = max(0.0, -bm25_rank)
            if max_raw > 0.0:
                norm_score = round(max(1e-6, raw_bm25 / max_raw), 6)
            else:
                norm_score = round(1.0 / (idx + 2), 6)
            scored_hits.append(
                CandidateHit(
                    note_id=nid,
                    path=str(note_row["path"]),
                    title=str(note_row["title"]),
                    note_type=note_row["type"] or "note",
                    score=norm_score,
                    excerpt=_make_excerpt(best_text, title=str(note_row["title"])),
                    source="fts_weak",
                    weak_evidence=True,
                )
            )
    else:
        scored_hits.sort(key=lambda h: h.score, reverse=True)

    return scored_hits, evidence_bypassed


def rank_and_budget(
    conn: sqlite3.Connection,
    trigger_hits: list[CandidateHit],
    fts_hits: list[CandidateHit],
    *,
    k: int,
    hops: int,
    budget_limit: int,
    cooldown_blocked: AbstractSet[str] = frozenset(),
    evidence_bypassed: bool = False,
) -> dict[str, Any]:
    """合并 Trigger 命中与 FTS 兜底命中，去重补齐至 k 条并执行图谱扩散与预算裁剪。"""
    seen_paths: set[str] = set()
    merged: list[CandidateHit] = []

    for hit in trigger_hits:
        if hit.path in cooldown_blocked or hit.path in seen_paths:
            continue
        seen_paths.add(hit.path)
        merged.append(hit)

    if len(merged) < k:
        for hit in fts_hits:
            if hit.path in cooldown_blocked or hit.path in seen_paths:
                continue
            seen_paths.add(hit.path)
            merged.append(hit)
            if len(merged) >= k:
                break

    seeds = merged[:k]
    materialized: list[dict[str, Any]] = []
    for cand in seeds:
        connected = _fetch_connected(conn, cand.note_id) if hops >= 1 else []
        source = "fts_weak" if (cand.weak_evidence and cand.source != "trigger") else cand.source
        item: dict[str, Any] = {
            "path": cand.path,
            "title": cand.title,
            "type": cand.note_type,
            "score": cand.score,
            "source": source,
            "excerpt": cand.excerpt,
            "connected": connected,
        }
        if cand.weak_evidence and cand.source != "trigger":
            item["weak_evidence"] = True
        materialized.append(item)

    final_hits: list[dict[str, Any]] = []
    truncated = False
    used_chars = 0
    for hit in materialized:
        hit_len = len(json.dumps(hit, ensure_ascii=False))
        if final_hits and (used_chars + hit_len > budget_limit):
            truncated = True
            break
        final_hits.append(hit)
        used_chars += hit_len

    if len(materialized) > len(final_hits) and len(final_hits) < len(seeds):
        truncated = True

    result: dict[str, Any] = {
        "hits": final_hits,
        "truncated": truncated,
        "total_chars": used_chars,
    }
    if final_hits and (evidence_bypassed or all(bool(h.get("weak_evidence")) for h in final_hits)) and all(
        bool(h.get("weak_evidence")) for h in final_hits
    ):
        result["evidence_gate"] = "bypassed"
    return result


def search_notes(
    config: AppConfig,
    query: str,
    today: dt.date | None = None,
    max_chars_override: int | None = None,
    k_override: int | None = None,
    hops_override: int | None = None,
    cooldown_blocked: AbstractSet[str] = frozenset(),
) -> dict[str, Any]:
    """执行三段式检索：Trigger 精确触发 -> FTS5 兜底补齐 -> 1 跳扩散与预算裁剪。"""
    db_path = config.broker.db_path
    if not db_path.exists():
        return {"hits": [], "truncated": False, "total_chars": 0}

    ref_date = today or dt.date.today()
    ret_cfg = config.memory.retrieval
    seed_k = max(1, k_override) if k_override is not None else ret_cfg.seed_k
    hops = max(0, hops_override) if hops_override is not None else ret_cfg.hops
    budget_limit = min(
        max_chars_override if max_chars_override is not None else ret_cfg.max_chars,
        ret_cfg.hard_max_chars,
    )

    with connect_db(db_path) as conn:
        total_notes = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        if total_notes == 0:
            return {"hits": [], "truncated": False, "total_chars": 0}

        trigger_hits = recall_trigger(conn, query, cooldown_blocked=cooldown_blocked)
        fts_hits, evidence_bypassed = recall_fts(
            conn,
            query,
            ret_cfg=ret_cfg,
            today=ref_date,
        )
        result = rank_and_budget(
            conn,
            trigger_hits,
            fts_hits,
            k=seed_k,
            hops=hops,
            budget_limit=budget_limit,
            cooldown_blocked=cooldown_blocked,
            evidence_bypassed=evidence_bypassed,
        )
        hit_paths = [str(h["path"]) for h in (result.get("hits") or []) if h.get("path")]
        if hit_paths:
            try:
                _ensure_notes_columns(conn)
                now_us = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1_000_000)
                conn.executemany(
                    "UPDATE notes SET last_access_us = MAX(COALESCE(last_access_us, 0), ?) WHERE path = ?",
                    [(now_us, p) for p in hit_paths],
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                _logger.debug("search_notes failed to update last_access_us (ignored): %s", exc)
        return result
