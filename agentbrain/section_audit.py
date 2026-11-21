"""夜间「小节体检」（section audit）：发现同文件内讲同一件事的重复小节，经模型判断后合并收口。"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig
from agentbrain.index import reindex
from agentbrain.proactive_apply import (
    MAX_DEPRECATED_LINES,
    _ASCII_TOKEN_RE,
    _CJK_RUN_RE,
    _DEPRECATED_LINE_PREFIX,
    _ManagedSection,
    _assert_safe_profile_subpath,
    _extract_managed_block,
    _merge_managed_block,
    _normalize_section_key,
    _parse_frontmatter_and_body,
    _parse_managed_sections,
    _render_managed_sections,
    _split_section_body_and_deprecated,
    _title_jaccard,
)
from agentbrain.proactive_clock import Clock, SystemClock, ensure_utc

MAX_PAIRS_PER_FILE = 6
CONTENT_SIM_MAX_CHARS = 400
SECTION_PREVIEW_CHARS = 120
MAX_RETIRED_SNIPPET_CHARS = 160


def _text_bigrams(text: str, *, max_chars: int = CONTENT_SIM_MAX_CHARS) -> set[str]:
    """对正文前 max_chars 字提取 CJK 二元组及长度>=2 的 ASCII 词。

    数字先归一到 `0`：同一条规则改了数值（40px→48px、120→150）时，
    差异恰恰落在数字上；不归一的话"同一件事的两个版本"会被判成不相似，
    正是最该合并的情况漏掉。候选只是给模型看的，误报由模型裁决。
    """
    sample = re.sub(r"\d+", "0", str(text or "")[:max_chars].lower())
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(sample):
        if len(run) == 1:
            grams.add(run)
        else:
            for i in range(len(run) - 1):
                grams.add(run[i : i + 2])
    for tok in _ASCII_TOKEN_RE.findall(sample):
        if len(tok) >= 2:
            grams.add(tok)
    return grams


def _content_jaccard(a: str, b: str, *, max_chars: int = CONTENT_SIM_MAX_CHARS) -> float:
    ga = _text_bigrams(a, max_chars=max_chars)
    gb = _text_bigrams(b, max_chars=max_chars)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def discover_audit_candidates(
    config: AppConfig,
    *,
    min_similarity: float | None = None,
    max_files: int | None = None,
) -> list[dict[str, Any]]:
    """扫描 20_LongTerm/**/*.md（不含 _Overview.md），找出同文件内高度相似的小节候选对。

    返回结构可直接用于 prompt 构造：
    `[{rel, title, sections: [{heading, key, preview}], pairs: [(i, j, score)]}]`
    """
    lt_dir = config.profile_dir / "20_LongTerm"
    if not lt_dir.exists() or lt_dir.is_symlink():
        return []

    eff_min_sim = (
        float(min_similarity)
        if min_similarity is not None
        else float(config.memory.proactive.section_audit_min_similarity)
    )
    eff_max_files = (
        max(1, int(max_files))
        if max_files is not None
        else max(1, int(config.memory.proactive.section_audit_max_files))
    )

    candidates: list[tuple[float, str, dict[str, Any]]] = []

    for md in sorted(lt_dir.rglob("*.md")):
        if md.name == "_Overview.md" or md.is_symlink() or not md.is_file():
            continue
        try:
            rel = md.relative_to(config.profile_dir).as_posix()
            _assert_safe_profile_subpath(config, rel)
        except Exception:
            continue

        raw_text = md.read_text(encoding="utf-8", errors="replace")
        managed = _extract_managed_block(raw_text)
        if not managed:
            continue

        _, raw_sections, _ = _parse_managed_sections(managed)
        if len(raw_sections) < 2:
            continue

        # 排除已被「> 已废止」覆盖的内容：取每个小节的现行正文，全废止/空正文小节不参与候选
        active_items: list[tuple[_ManagedSection, str]] = []
        for sec in raw_sections:
            active_body, _ = _split_section_body_and_deprecated(sec.body)
            if not active_body.strip():
                continue
            active_items.append((sec, active_body))

        if len(active_items) < 2:
            continue

        pairs: list[tuple[int, int, float]] = []
        n = len(active_items)
        for i in range(n):
            sec_i, body_i = active_items[i]
            for j in range(i + 1, n):
                sec_j, body_j = active_items[j]
                t_sim = _title_jaccard(sec_i.heading, sec_j.heading)
                c_sim = _content_jaccard(body_i, body_j, max_chars=CONTENT_SIM_MAX_CHARS)
                score = max(t_sim, c_sim)
                if score >= eff_min_sim:
                    pairs.append((i, j, round(score, 4)))

        if not pairs:
            continue

        pairs.sort(key=lambda item: (-item[2], item[0], item[1]))
        pairs = pairs[:MAX_PAIRS_PER_FILE]

        fm, _ = _parse_frontmatter_and_body(raw_text)
        note_title = str(fm.get("title") or "").strip() or md.stem

        sections_payload = [
            {
                "heading": sec.heading,
                "key": sec.section_key or "",
                "preview": " ".join(body.split())[:SECTION_PREVIEW_CHARS],
            }
            for sec, body in active_items
        ]
        cand_obj = {
            "rel": rel,
            "title": note_title,
            "sections": sections_payload,
            "pairs": pairs,
        }
        candidates.append((pairs[0][2], rel, cand_obj))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in candidates[:eff_max_files]]


def build_section_audit_prompt(candidate: dict[str, Any]) -> str:
    """为单个候选文件构造模型判断的 user prompt。"""
    rel = str(candidate.get("rel") or "")
    title = str(candidate.get("title") or "")
    sections = candidate.get("sections") or []
    pairs = candidate.get("pairs") or []

    lines: list[str] = [
        f"文件：{rel}",
        f"标题：{title}",
        "",
        "### 当前小节列表",
    ]
    for idx, sec in enumerate(sections):
        heading = str(sec.get("heading") or "")
        key = str(sec.get("key") or "") or "(无)"
        preview = str(sec.get("preview") or "")
        lines.append(f"[{idx}] 标题：{heading} | section_key: {key}")
        lines.append(f"    正文预览：{preview}")

    lines.append("")
    lines.append("### 候选相似对")
    for i, j, score in pairs:
        h_i = sections[i]["heading"] if 0 <= i < len(sections) else str(i)
        h_j = sections[j]["heading"] if 0 <= j < len(sections) else str(j)
        lines.append(f"- [{i}]「{h_i}」 <-> [{j}]「{h_j}」（相似度 {score:.4f}）")

    lines.append("")
    lines.append("请判断以上候选对中哪些确实在讲同一件事需要合并，并严格按 JSON 格式输出。")
    return "\n".join(lines)


def _loads_json_loose(text: str) -> Any:
    """宽容解析模型输出：去 ``` 围栏、截取最外层 {...}。

    模型很爱把 JSON 包进代码块或用一句话开头；严格 json.loads 会让这些
    输出被静默跳过（体检等于永远不合并），所以先剥壳再解析。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json|JSON)?\s*(.+?)```", raw, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def parse_audit_response(
    raw: Any,
    valid_headings: set[str],
) -> list[dict[str, Any]] | None:
    """解析并校验单文件的模型输出；非法 JSON/结构返回 None（调用方跳过该文件，不算失败）。"""
    obj = raw
    if isinstance(obj, str):
        obj = _loads_json_loose(obj)
        if obj is None:
            return None
    if not isinstance(obj, dict):
        return None
    raw_merges = obj.get("merges")
    if not isinstance(raw_merges, list):
        return None

    cleaned: list[dict[str, Any]] = []
    retired_so_far: set[str] = set()

    for item in raw_merges:
        if not isinstance(item, dict):
            continue
        keep = str(item.get("keep") or "").strip()
        if not keep or keep not in valid_headings or keep in retired_so_far:
            continue
        raw_retire = item.get("retire")
        if not isinstance(raw_retire, list):
            continue
        retire_list: list[str] = []
        seen_retire: set[str] = set()
        for r in raw_retire:
            r_clean = str(r or "").strip()
            if (
                not r_clean
                or r_clean == keep
                or r_clean not in valid_headings
                or r_clean in retired_so_far
                or r_clean in seen_retire
            ):
                continue
            seen_retire.add(r_clean)
            retire_list.append(r_clean)
        if not retire_list:
            continue

        merged_content = str(item.get("merged_content") or "").strip()
        if not merged_content:
            continue
        if len(merged_content) > 300:
            merged_content = merged_content[:300].rstrip()

        sec_key = _normalize_section_key(item.get("section_key"))
        retired_so_far.update(retire_list)
        cleaned.append(
            {
                "keep": keep,
                "retire": retire_list,
                "merged_content": merged_content,
                "section_key": sec_key,
            }
        )

    return cleaned


def apply_audit(
    config: AppConfig,
    merges_by_file: dict[str, Any],
    *,
    today_str: str = "",
) -> list[dict[str, Any]]:
    """按模型决策合并受管块内的重复小节，只改受管块，无变化不写盘。

    返回每条改动的文件摘要列表：`[{"rel": str, "merges": [{"keep": str, "retired": list[str], "retired_count": int}]}]`。
    """
    if not merges_by_file:
        return []

    eff_today = str(today_str or "").strip() or dt.date.today().isoformat()
    summaries: list[dict[str, Any]] = []

    for rel, raw_entry in merges_by_file.items():
        if isinstance(raw_entry, dict) and isinstance(raw_entry.get("merges"), list):
            merge_list = raw_entry["merges"]
        elif isinstance(raw_entry, list):
            merge_list = raw_entry
        else:
            continue

        if not merge_list:
            continue

        target = _assert_safe_profile_subpath(config, rel)
        if not target.exists() or target.is_symlink() or not target.is_file():
            continue

        original_text = target.read_text(encoding="utf-8")
        managed = _extract_managed_block(original_text)
        if managed is None:
            continue

        preamble, sections, link_lines = _parse_managed_sections(managed)
        if len(sections) < 2:
            continue

        valid_headings = {sec.heading.strip() for sec in sections}
        validated_merges = parse_audit_response({"merges": merge_list}, valid_headings)
        if not validated_merges:
            continue

        working_sections = list(sections)
        file_merge_records: list[dict[str, Any]] = []

        for grp in validated_merges:
            keep_heading = grp["keep"]
            retire_headings: list[str] = grp["retire"]
            merged_content: str = grp["merged_content"]
            given_key: str | None = grp.get("section_key")

            keep_idx = next(
                (idx for idx, s in enumerate(working_sections) if s.heading.strip() == keep_heading),
                None,
            )
            if keep_idx is None:
                continue

            retire_indices: list[int] = []
            for r_head in retire_headings:
                r_idx = next(
                    (
                        idx
                        for idx, s in enumerate(working_sections)
                        if idx != keep_idx and s.heading.strip() == r_head
                    ),
                    None,
                )
                if r_idx is not None and r_idx not in retire_indices:
                    retire_indices.append(r_idx)

            if not retire_indices:
                continue

            keep_sec = working_sections[keep_idx]
            _, deprecated_lines = _split_section_body_and_deprecated(keep_sec.body)

            actually_retired: list[str] = []
            for r_idx in retire_indices:
                r_sec = working_sections[r_idx]
                r_active, _ = _split_section_body_and_deprecated(r_sec.body)
                snippet = " ".join(r_active.split())[:MAX_RETIRED_SNIPPET_CHARS]
                deprecated_lines.append(
                    f"{_DEPRECATED_LINE_PREFIX}{eff_today}）：{r_sec.heading}：{snippet}"
                )
                actually_retired.append(r_sec.heading)

            deprecated_lines = deprecated_lines[-MAX_DEPRECATED_LINES:]
            if deprecated_lines:
                new_body = f"{merged_content}\n\n" + "\n".join(deprecated_lines)
            else:
                new_body = merged_content

            resolved_key = given_key if given_key else keep_sec.section_key
            working_sections[keep_idx] = _ManagedSection(
                heading=keep_sec.heading,
                body=new_body,
                section_key=resolved_key,
            )

            retire_idx_set = set(retire_indices)
            working_sections = [
                sec for idx, sec in enumerate(working_sections) if idx not in retire_idx_set
            ]
            file_merge_records.append(
                {
                    "keep": keep_sec.heading,
                    "retired": actually_retired,
                    "retired_count": len(actually_retired),
                }
            )

        if not file_merge_records:
            continue

        new_managed = _render_managed_sections(preamble, working_sections, link_lines)
        new_text = _merge_managed_block(
            original_text,
            default_header="",
            managed_body=new_managed,
        )
        if new_text == original_text:
            continue

        atomic_write_text(target, new_text)
        summaries.append({"rel": rel, "merges": file_merge_records})

    return summaries


def run_section_audit(
    config: AppConfig,
    *,
    job_id: int = 0,
    clock: Clock | None = None,
    write_lock: threading.Lock | None = None,
    hermes_runner: Callable[..., dict[str, Any]] | None = None,
    model_caller: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """执行一次完整的夜间小节体检：候选发现 -> 逐文件模型判断 -> 应用 -> 写审计日志。"""
    if not config.memory.proactive.section_audit_enabled:
        return []

    eff_clock = clock or SystemClock()
    now_utc = ensure_utc(eff_clock.now_utc())
    today_str = eff_clock.to_local(now_utc).date().isoformat()

    candidates = discover_audit_candidates(config)
    merges_by_file: dict[str, list[dict[str, Any]]] = {}

    if candidates:
        if model_caller is None:
            from agentbrain.proactive_worker import call_proactive_model as _default_caller

            caller = _default_caller
        else:
            caller = model_caller

        max_calls = max(1, int(config.memory.proactive.section_audit_max_files))
        for cand in candidates[:max_calls]:
            user_prompt = build_section_audit_prompt(cand)
            resp = caller(
                config,
                user_prompt,
                hermes_runner=hermes_runner,
                job_type="section_audit",
            )
            valid_headings = {str(s["heading"]).strip() for s in cand["sections"]}
            parsed = parse_audit_response(resp, valid_headings)
            if parsed is None:
                # 模型没给合法 JSON：跳过该文件，不算失败
                continue
            if parsed:
                merges_by_file[cand["rel"]] = parsed

    if write_lock is not None:
        with write_lock:
            applied = apply_audit(config, merges_by_file, today_str=today_str)
            if applied:
                reindex(config)
    else:
        applied = apply_audit(config, merges_by_file, today_str=today_str)
        if applied:
            reindex(config)

    config.broker.ensure_runtime_dirs()
    audit_file = config.broker.audit_dir / "section_audit.jsonl"
    record = {
        "ts": now_utc.isoformat(),
        "job_id": int(job_id),
        "files": [{"rel": item["rel"], "merges": item["merges"]} for item in applied],
    }
    with audit_file.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return applied
