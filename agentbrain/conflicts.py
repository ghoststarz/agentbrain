"""确定性 Git 冲突裁决器与同步冲突回执队列（不调用任何模型，不执行任何 git 命令）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from agentbrain.atomic import atomic_write_text

MANAGED_START = "<!-- agentbrain:managed:start -->"
MANAGED_END = "<!-- agentbrain:managed:end -->"

_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}(?:\s|$)|={7}$|>{7}(?:\s|$))", re.MULTILINE)
_DIARY_TS_RE = re.compile(r"^-\s*\[(\d{2}:\d{2}:\d{2})\]")
_SEQ_NAME_RE = re.compile(r"^([A-Za-z]+)-(\d+)(.*)$")
_H3_LINE_RE = re.compile(r"^###\s+(.+?)\s*$")
_LONGTERM_SEQ_SUBDIRS = frozenset({"decisions", "facts", "entities", "projects"})

_DEFAULT_HOT_MAX_CHARS = 1500
_MAX_NOTICE_TEXT_CHARS = 120


def default_notices_path() -> Path:
    """返回默认冲突回执队列路径：%LOCALAPPDATA%\\agentbrain\\state\\notices.jsonl。"""
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        return Path(local_app) / "agentbrain" / "state" / "notices.jsonl"
    return Path.home() / "AppData" / "Local" / "agentbrain" / "state" / "notices.jsonl"


def _truncate_notice_text(text: str, max_chars: int = _MAX_NOTICE_TEXT_CHARS) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[: max(1, max_chars - 1)].rstrip() + "…"


def append_notice(
    notices_path: Path,
    *,
    kind: str,
    text: str,
    ts: str | None = None,
    notice_id: str | None = None,
) -> dict[str, Any]:
    """向 notices.jsonl 追加一条未投递回执记录。"""
    clean_kind = "severe" if str(kind or "").strip().lower() == "severe" else "conflict"
    entry: dict[str, Any] = {
        "id": notice_id or uuid.uuid4().hex[:12],
        "ts": ts or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "kind": clean_kind,
        "text": _truncate_notice_text(text),
        "delivered_at": None,
    }
    notices_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with notices_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
    return entry


def _read_notice_entries(notices_path: Path) -> list[dict[str, Any]]:
    if not notices_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in notices_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def list_pending_notices(notices_path: Path, *, limit: int = 3) -> list[dict[str, Any]]:
    """读取尚未投递（delivered_at 为空）的回执，最多返回 limit 条。"""
    if limit <= 0 or not notices_path.exists():
        return []
    pending: list[dict[str, Any]] = []
    for entry in _read_notice_entries(notices_path):
        if entry.get("delivered_at"):
            continue
        nid = str(entry.get("id") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not nid or not text:
            continue
        pending.append(
            {
                "id": nid,
                "ts": str(entry.get("ts") or ""),
                "kind": str(entry.get("kind") or "conflict"),
                "text": text,
            }
        )
        if len(pending) >= limit:
            break
    return pending


def ack_notices(notices_path: Path, ids: list[str]) -> int:
    """将指定 id 的回执标记为已投递，原子写回 notices.jsonl。"""
    id_set = {str(x).strip() for x in ids if str(x).strip()}
    if not id_set or not notices_path.exists():
        return 0
    entries = _read_notice_entries(notices_path)
    if not entries:
        return 0
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    acked = 0
    for entry in entries:
        nid = str(entry.get("id") or "").strip()
        if nid in id_set and not entry.get("delivered_at"):
            entry["delivered_at"] = now_iso
            acked += 1
    if acked > 0:
        lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries]
        atomic_write_text(notices_path, "\n".join(lines) + "\n")
    return acked


def has_conflict_markers(text: str) -> bool:
    """检查文本中是否存在 Git 冲突标记行（<<<<<<< / ======= / >>>>>>>）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONFLICT_MARKER_RE.search(normalized) is not None


def parse_conflict_sides(text: str) -> tuple[str, str, int] | None:
    """解析 git rebase 冲突文件，返回 (remote_text, local_text, conflict_count)。

    在 `git pull --rebase` 中：
    - `<<<<<<< HEAD` 到 `=======` 之间为远端（upstream / onto）侧；
    - `|||||||` 到 `=======` 之间为 diff3 共同祖先（忽略）；
    - `=======` 到 `>>>>>>>` 之间为本地（replay）侧。

    若冲突标记不完整/嵌套异常，返回 None。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # 保留原始末尾换行语义
    had_trailing_nl = normalized.endswith("\n")
    if had_trailing_nl and lines and lines[-1] == "":
        lines = lines[:-1]

    remote_lines: list[str] = []
    local_lines: list[str] = []
    state = "normal"  # normal | remote | base | local
    conflict_count = 0

    for line in lines:
        if line.startswith("<<<<<<<") and (len(line) == 7 or line[7].isspace()):
            if state != "normal":
                return None
            state = "remote"
            continue
        if line.startswith("|||||||") and (len(line) == 7 or line[7].isspace()):
            if state != "remote":
                return None
            state = "base"
            continue
        if line == "=======":
            if state not in ("remote", "base"):
                return None
            state = "local"
            continue
        if line.startswith(">>>>>>>") and (len(line) == 7 or line[7].isspace()):
            if state != "local":
                return None
            state = "normal"
            conflict_count += 1
            continue

        if state == "normal":
            remote_lines.append(line)
            local_lines.append(line)
        elif state == "remote":
            remote_lines.append(line)
        elif state == "local":
            local_lines.append(line)
        # state == "base": 忽略 diff3 base 段

    if state != "normal":
        return None

    remote_text = "\n".join(remote_lines) + ("\n" if had_trailing_nl and remote_lines else "")
    local_text = "\n".join(local_lines) + ("\n" if had_trailing_nl and local_lines else "")
    return remote_text, local_text, conflict_count


def _read_hot_max_chars(vault_root: Path, rel_posix: str, default_profile: str = "lily") -> int:
    """从对应 profile 的 memory.yaml 读取 working.hot_max_chars，读不到则回退默认值。"""
    parts = Path(rel_posix).parts
    profile = default_profile
    if len(parts) >= 2 and parts[0].lower() == "profiles":
        profile = parts[1]
    candidates = [
        vault_root / "profiles" / profile / "memory.yaml",
        vault_root / "memory.yaml",
    ]
    for cfg_path in candidates:
        if not cfg_path.exists():
            continue
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                working = raw.get("working")
                if isinstance(working, dict) and working.get("hot_max_chars") is not None:
                    val = int(working["hot_max_chars"])
                    if val > 0:
                        return val
        except Exception:
            continue
    return _DEFAULT_HOT_MAX_CHARS


def _resolve_diary(remote_text: str, local_text: str) -> str:
    """规则 1：30_Diary 冲突 → 两侧按行合并后按时间戳排序去重。"""
    headers: list[str] = []
    seen_headers: set[str] = set()
    entries: list[tuple[str, int, str]] = []
    seen_entries: set[str] = set()
    tail_lines: list[str] = []
    seen_tail: set[str] = set()

    order_idx = 0
    for side in (remote_text, local_text):
        in_entries = False
        for raw_line in side.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            m = _DIARY_TS_RE.match(line)
            if m:
                in_entries = True
                if line not in seen_entries:
                    seen_entries.add(line)
                    entries.append((m.group(1), order_idx, line))
                    order_idx += 1
            elif not in_entries and line.startswith("#"):
                if line not in seen_headers:
                    seen_headers.add(line)
                    headers.append(line)
            else:
                if line not in seen_tail:
                    seen_tail.add(line)
                    tail_lines.append(line)

    entries.sort(key=lambda item: (item[0], item[1]))
    out_parts: list[str] = []
    if headers:
        out_parts.extend(headers)
        if entries or tail_lines:
            out_parts.append("")
    out_parts.extend(item[2] for item in entries)
    if tail_lines:
        if entries:
            out_parts.append("")
        out_parts.extend(tail_lines)
    return ("\n".join(out_parts) + "\n") if out_parts else ""


def _parse_iso_ts(raw: Any) -> dt.datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _is_newer_task(local_entry: Any, remote_entry: Any) -> bool:
    """判断 local_entry 的 updated_at 是否严格晚于 remote_entry。"""
    if not isinstance(local_entry, dict):
        return False
    if not isinstance(remote_entry, dict):
        return True
    l_raw = str(local_entry.get("updated_at") or "").strip()
    r_raw = str(remote_entry.get("updated_at") or "").strip()
    l_dt = _parse_iso_ts(l_raw)
    r_dt = _parse_iso_ts(r_raw)
    if l_dt is not None and r_dt is not None:
        return l_dt > r_dt
    return l_raw > r_raw


def _resolve_handoff(remote_text: str, local_text: str) -> str | None:
    """规则 2：.state/handoff.json → 按 tasks.<task_key> 合并，同 key 取 updated_at 较新者。"""
    try:
        remote_obj = json.loads(remote_text)
        local_obj = json.loads(local_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(remote_obj, dict) or not isinstance(local_obj, dict):
        return None
    remote_tasks = remote_obj.get("tasks")
    local_tasks = local_obj.get("tasks")
    if remote_tasks is None:
        remote_tasks = {}
    if local_tasks is None:
        local_tasks = {}
    if not isinstance(remote_tasks, dict) or not isinstance(local_tasks, dict):
        return None

    merged_obj: dict[str, Any] = dict(local_obj)
    merged_obj.update(remote_obj)
    merged_tasks: dict[str, Any] = {}
    all_keys = list(remote_tasks.keys()) + [k for k in local_tasks.keys() if k not in remote_tasks]
    for key in all_keys:
        in_r = key in remote_tasks
        in_l = key in local_tasks
        if in_r and not in_l:
            merged_tasks[key] = remote_tasks[key]
        elif in_l and not in_r:
            merged_tasks[key] = local_tasks[key]
        else:
            if _is_newer_task(local_tasks[key], remote_tasks[key]):
                merged_tasks[key] = local_tasks[key]
            else:
                merged_tasks[key] = remote_tasks[key]
    merged_obj["tasks"] = merged_tasks
    return json.dumps(merged_obj, ensure_ascii=False, indent=2) + "\n"


def _split_paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [p.strip() for p in re.split(r"\n[ \t]*\n+", normalized) if p.strip()]


def _resolve_hot_md(remote_text: str, local_text: str, *, hot_max_chars: int) -> str:
    """规则 3：10_Working/HOT.md → 空行分段并集去重（远端在前），超限时从尾部按段截断，不写注释。"""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for p in _split_paragraphs(remote_text) + _split_paragraphs(local_text):
        if p not in seen:
            seen.add(p)
            paragraphs.append(p)

    if not paragraphs:
        return ""

    limit = max(1, int(hot_max_chars))
    while len(paragraphs) > 1 and len("\n\n".join(paragraphs)) > limit:
        paragraphs.pop()

    joined = "\n\n".join(paragraphs)
    if len(joined) > limit:
        joined = joined[:limit].rstrip()
    return joined + "\n"


def _extract_frontmatter_title_and_h1(text: str) -> tuple[str, str]:
    """提取 Markdown frontmatter 中的 title 与正文第一个一级标题。"""
    fm_title = ""
    body = text
    if text.startswith("---\n"):
        lines = text.splitlines(keepends=True)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                try:
                    fm = yaml.safe_load("".join(lines[1:i])) or {}
                    if isinstance(fm, dict) and fm.get("title"):
                        fm_title = str(fm["title"]).strip()
                except Exception:
                    pass
                body = "".join(lines[i + 1 :])
                break
    h1 = ""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            h1 = s[2:].strip()
            break
    return fm_title, h1


def _is_whole_file_conflict(raw_text: str) -> bool:
    """判断除冲突块外是否没有任何实质性的共同正文行。"""
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    state = "normal"
    normal_nonempty = 0
    for line in normalized.splitlines():
        if line.startswith("<<<<<<<") and (len(line) == 7 or line[7].isspace()):
            state = "in"
            continue
        if line.startswith(">>>>>>>") and (len(line) == 7 or line[7].isspace()):
            state = "normal"
            continue
        if state == "normal" and line.strip():
            normal_nonempty += 1
    return normal_nonempty == 0


def _is_longterm_seq_add_add(rel_posix: str, raw_text: str, remote_text: str, local_text: str) -> bool:
    """判断是否属于 20_LongTerm/{Decisions,Facts,Entities,Projects}/ 下同号新笔记（add/add）。"""
    parts = [p.lower() for p in Path(rel_posix).parts]
    if not rel_posix.lower().endswith(".md"):
        return False
    try:
        lt_idx = parts.index("20_longterm")
    except ValueError:
        return False
    if lt_idx + 2 >= len(parts):
        return False
    if parts[lt_idx + 1] not in _LONGTERM_SEQ_SUBDIRS:
        return False

    stem = Path(rel_posix).stem
    m = _SEQ_NAME_RE.match(stem)
    if not m:
        return False
    digits = m.group(2)
    if len(digits) == 8 and 19000101 <= int(digits) <= 29991231:
        return False

    r_fm_title, r_h1 = _extract_frontmatter_title_and_h1(remote_text)
    l_fm_title, l_h1 = _extract_frontmatter_title_and_h1(local_text)
    if r_fm_title and l_fm_title:
        return r_fm_title != l_fm_title
    if r_h1 and l_h1:
        return r_h1 != l_h1
    if MANAGED_START in remote_text or MANAGED_START in local_text:
        return False
    return _is_whole_file_conflict(raw_text)


def _allocate_next_seq_in_dir(target_dir: Path, prefix: str, *, reserved_names: set[str] | None = None) -> int:
    """扫描同目录下同前缀文件，分配下一个可用序号（跳过 8 位日期前缀）。"""
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:-|$)", re.IGNORECASE)
    max_seq = 0
    if target_dir.exists():
        for md in target_dir.glob("*.md"):
            if md.is_symlink():
                continue
            m = pat.match(md.stem)
            if not m:
                continue
            digits = m.group(1)
            val = int(digits)
            if len(digits) == 8 and 19000101 <= val <= 29991231:
                continue
            if val > max_seq:
                max_seq = val
    if reserved_names:
        for name in reserved_names:
            stem = Path(name).stem
            m = pat.match(stem)
            if not m:
                continue
            digits = m.group(1)
            val = int(digits)
            if len(digits) == 8 and 19000101 <= val <= 29991231:
                continue
            if val > max_seq:
                max_seq = val
    return max_seq + 1


def _format_seq(seq: int) -> str:
    """两位补零（01–99），100 起自然三位（100）。"""
    return f"{max(1, int(seq)):02d}"


def _split_h3_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """把 Markdown 按 `### ` 标题切分为 (前导文本, [(标题, 完整块文本), ...])。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if normalized.endswith("\n") and lines and lines[-1] == "":
        lines = lines[:-1]

    preamble_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur_title: str | None = None
    cur_lines: list[str] = []

    for line in lines:
        m = _H3_LINE_RE.match(line)
        if m:
            if cur_title is not None:
                sections.append((cur_title, cur_lines))
            cur_title = m.group(1).strip()
            cur_lines = [line]
        else:
            if cur_title is None:
                preamble_lines.append(line)
            else:
                cur_lines.append(line)

    if cur_title is not None:
        sections.append((cur_title, cur_lines))

    preamble = "\n".join(preamble_lines).rstrip("\n")
    rendered_sections = [(title, "\n".join(sec_lines).rstrip("\n")) for title, sec_lines in sections]
    return preamble, rendered_sections


def _merge_h3_text(remote_text: str, local_text: str) -> tuple[str, bool]:
    """按 `### ` 切块合并：不同标题并集，同标题取远端块。返回 (merged, has_any_h3)。"""
    r_pre, r_secs = _split_h3_sections(remote_text)
    _, l_secs = _split_h3_sections(local_text)
    if not r_secs and not l_secs:
        return remote_text, False

    seen_titles: set[str] = set()
    merged_blocks: list[str] = []
    for title, block in r_secs:
        if title not in seen_titles:
            seen_titles.add(title)
            merged_blocks.append(block)
    for title, block in l_secs:
        if title not in seen_titles:
            seen_titles.add(title)
            merged_blocks.append(block)

    parts: list[str] = []
    if r_pre.strip():
        parts.append(r_pre)
    parts.extend(merged_blocks)
    return "\n\n".join(parts) + "\n", True


def _extract_managed_parts(text: str) -> tuple[str, str, str] | None:
    """提取 (before, inside, after)；若标记缺失或不成对返回 None。"""
    s_idx = text.find(MANAGED_START)
    e_idx = text.find(MANAGED_END)
    if s_idx == -1 or e_idx == -1 or e_idx < s_idx:
        return None
    before = text[:s_idx]
    inside = text[s_idx + len(MANAGED_START) : e_idx]
    after = text[e_idx + len(MANAGED_END) :]
    return before, inside, after


def _strip_managed_markers(text: str) -> str:
    return text.replace(MANAGED_START, "").replace(MANAGED_END, "")


def _resolve_longterm_md(remote_text: str, local_text: str) -> str | None:
    """规则 5：其它 20_LongTerm/**/*.md → 按 ### 切块合并，并保证受管标记完整且唯一。"""
    has_managed = (
        MANAGED_START in remote_text
        or MANAGED_END in remote_text
        or MANAGED_START in local_text
        or MANAGED_END in local_text
    )

    if has_managed:
        r_parts = _extract_managed_parts(remote_text)
        l_parts = _extract_managed_parts(local_text)
        if r_parts is None and l_parts is None:
            return None
        if r_parts is not None:
            r_before, r_inside, r_after = r_parts
            l_inside = l_parts[1] if l_parts is not None else _strip_managed_markers(local_text)
        else:
            assert l_parts is not None
            r_before, _, r_after = l_parts
            r_inside = _strip_managed_markers(remote_text)
            l_inside = l_parts[1]

        r_inside_clean = _strip_managed_markers(r_inside)
        l_inside_clean = _strip_managed_markers(l_inside)
        merged_inside, inside_has_h3 = _merge_h3_text(r_inside_clean, l_inside_clean)
        if not inside_has_h3:
            # 受管块内没有 ### 标题：取远端受管块（保留受管标记）
            merged_inside = r_inside_clean.strip()
        else:
            merged_inside = merged_inside.strip()

        clean_before = _strip_managed_markers(r_before)
        clean_after = _strip_managed_markers(r_after)
        # 若 before 存在 ### 块且本地 before 也有独有 ### 块，也做并集
        if l_parts is not None:
            l_before_clean = _strip_managed_markers(l_parts[0])
            merged_before, before_has_h3 = _merge_h3_text(clean_before, l_before_clean)
            if before_has_h3:
                clean_before = merged_before
            l_after_clean = _strip_managed_markers(l_parts[2])
            merged_after, after_has_h3 = _merge_h3_text(clean_after, l_after_clean)
            if after_has_h3:
                clean_after = "\n" + merged_after.lstrip("\n")

        if clean_before and not clean_before.endswith("\n"):
            clean_before += "\n"
        block = f"{MANAGED_START}\n{merged_inside}\n{MANAGED_END}"
        if clean_after and not clean_after.startswith("\n"):
            clean_after = "\n" + clean_after
        result = f"{clean_before}{block}{clean_after}"
        if not result.endswith("\n"):
            result += "\n"

        # 严格校验受管标记恰好出现一次且顺序正确
        if (
            result.count(MANAGED_START) != 1
            or result.count(MANAGED_END) != 1
            or result.find(MANAGED_START) >= result.find(MANAGED_END)
        ):
            return None
        return result

    merged, has_h3 = _merge_h3_text(remote_text, local_text)
    if not has_h3:
        return None
    return merged


def _scan_conflicted_rel_paths(vault_root: Path) -> list[str]:
    """扫描 vault 内所有含 Git 冲突标记的非 .git 文本文件，返回相对路径列表（正斜杠）。"""
    found: list[str] = []
    root = vault_root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(part == ".git" for part in rel.parts):
            continue
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw_bytes:
            continue
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if has_conflict_markers(text):
            found.append(rel.as_posix())
    return found


def _build_notice_summary(
    resolved: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """汇总裁决结果为 (kind, text)，≤120 字中文。无冲突返回 None。"""
    if not resolved and not unresolved:
        return None
    rule_counts: dict[str, int] = {}
    for item in resolved:
        r = str(item.get("rule") or "auto")
        rule_counts[r] = rule_counts.get(r, 0) + 1
    rule_brief = "、".join(f"{k}×{v}" for k, v in sorted(rule_counts.items()))

    if unresolved:
        u_cnt = len(unresolved)
        r_cnt = len(resolved)
        if r_cnt > 0:
            msg = f"同步冲突需处理：{u_cnt} 个文件未自动解决（已归档本地副本），另有 {r_cnt} 个已按规则裁决（{rule_brief}）。"
        else:
            msg = f"同步冲突需处理：{u_cnt} 个文件无法自动裁决，已保留远端并归档本地副本至 90_Archive/_conflicts。"
        return "severe", _truncate_notice_text(msg)

    r_cnt = len(resolved)
    msg = f"同步冲突已自动裁决 {r_cnt} 个文件（{rule_brief}）。"
    return "conflict", _truncate_notice_text(msg)


def resolve_conflicts(
    vault_root: Path | str,
    rel_paths: list[str] | None = None,
    *,
    dry_run: bool = False,
    profile: str = "lily",
    notices_path: Path | None = None,
    write_notices: bool = True,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """对 git rebase 冲突后的工作树执行确定性裁决。

    只读文件、写文件，不执行任何 git 命令。
    """
    root = Path(vault_root).resolve()
    if rel_paths is None:
        targets = _scan_conflicted_rel_paths(root)
    else:
        targets = [str(p).replace("\\", "/").lstrip("./") for p in rel_paths if str(p).strip()]

    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    reserved_by_dir: dict[Path, set[str]] = {}

    for rel_posix in targets:
        file_path = (root / Path(rel_posix)).resolve()
        if not file_path.is_relative_to(root):
            unresolved.append(
                {
                    "path": rel_posix,
                    "rule": "archive_unresolved",
                    "note": "路径越界，拒绝处理",
                }
            )
            continue
        if not file_path.exists() or not file_path.is_file():
            continue

        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        if not has_conflict_markers(raw_text):
            # 幂等：无冲突标记的文件直接跳过
            continue

        parsed = parse_conflict_sides(raw_text)
        if parsed is None:
            # 冲突标记损坏：直接兜底归档原文
            archive_rel = f"90_Archive/_conflicts/{stamp}/{rel_posix}"
            if not dry_run:
                atomic_write_text(root / Path(archive_rel), raw_text)
            unresolved.append(
                {
                    "path": rel_posix,
                    "rule": "archive_unresolved",
                    "archive_path": archive_rel,
                    "note": "冲突标记不完整或嵌套异常，已归档原冲突文本",
                }
            )
            continue

        remote_text, local_text, conflict_count = parsed
        if conflict_count == 0:
            continue

        lower_rel = f"/{rel_posix.lower()}"
        rule_name = ""
        note = ""
        final_text: str | None = None
        extra_writes: list[tuple[Path, str]] = []

        # 规则 1：路径含 /30_Diary/
        if "/30_diary/" in lower_rel:
            rule_name = "diary_union_sort"
            final_text = _resolve_diary(remote_text, local_text)
            note = "日记两侧按行合并并按时间戳排序去重"

        # 规则 2：以 .state/handoff.json 结尾
        elif lower_rel.endswith("/.state/handoff.json") or rel_posix.lower() == ".state/handoff.json":
            rule_name = "handoff_json_merge"
            final_text = _resolve_handoff(remote_text, local_text)
            note = "handoff.json 按 task_key 合并，同 key 取 updated_at 较新者"

        # 规则 3：以 10_Working/HOT.md 结尾
        elif lower_rel.endswith("/10_working/hot.md") or rel_posix.lower() == "10_working/hot.md":
            rule_name = "hot_md_paragraph_union"
            hot_max = _read_hot_max_chars(root, rel_posix, default_profile=profile)
            final_text = _resolve_hot_md(remote_text, local_text, hot_max_chars=hot_max)
            note = f"HOT.md 段落并集去重（上限 {hot_max} 字）"

        # 规则 4：20_LongTerm/{Decisions,Facts,Entities,Projects}/ 下同号新笔记（add/add）
        elif _is_longterm_seq_add_add(rel_posix, raw_text, remote_text, local_text):
            rule_name = "longterm_add_add_renumber"
            m = _SEQ_NAME_RE.match(file_path.stem)
            assert m is not None
            prefix, _, rest = m.group(1), m.group(2), m.group(3)
            reserved = reserved_by_dir.setdefault(file_path.parent, set())
            next_seq = _allocate_next_seq_in_dir(file_path.parent, prefix, reserved_names=reserved)
            new_name = f"{prefix}-{_format_seq(next_seq)}{rest}{file_path.suffix}"
            while (file_path.parent / new_name).exists() or new_name in reserved:
                next_seq += 1
                new_name = f"{prefix}-{_format_seq(next_seq)}{rest}{file_path.suffix}"
            reserved.add(new_name)
            new_file_path = file_path.parent / new_name
            new_rel_posix = new_file_path.relative_to(root).as_posix()
            final_text = remote_text
            extra_writes.append((new_file_path, local_text))
            note = f"保留远端为原名，本地改存为 {new_rel_posix}"

        # 规则 5：其它 20_LongTerm/**/*.md 文本冲突
        elif "/20_longterm/" in lower_rel and lower_rel.endswith(".md"):
            rule_name = "longterm_section_merge"
            final_text = _resolve_longterm_md(remote_text, local_text)
            note = "长期笔记按 ### 标题合并并保留唯一受管标记"

        # 校验：若裁决失败或结果仍含冲突标记，强制转规则 6 兜底
        extra_has_markers = any(has_conflict_markers(content) for _, content in extra_writes)
        if final_text is None or has_conflict_markers(final_text) or extra_has_markers:
            archive_rel = f"90_Archive/_conflicts/{stamp}/{rel_posix}"
            if not dry_run:
                if not has_conflict_markers(remote_text):
                    atomic_write_text(file_path, remote_text)
                atomic_write_text(root / Path(archive_rel), local_text)
            unresolved.append(
                {
                    "path": rel_posix,
                    "rule": "archive_unresolved",
                    "archive_path": archive_rel,
                    "note": "无法安全自动裁决，已保留远端并归档本地完整内容",
                }
            )
            continue

        if not dry_run:
            atomic_write_text(file_path, final_text)
            for extra_path, extra_content in extra_writes:
                atomic_write_text(extra_path, extra_content)

        resolved.append(
            {
                "path": rel_posix,
                "rule": rule_name,
                "note": note,
            }
        )

    notices_out: list[dict[str, Any]] = []
    summary = _build_notice_summary(resolved, unresolved)
    if summary is not None:
        n_kind, n_text = summary
        if dry_run or not write_notices:
            notices_out.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "kind": n_kind,
                    "text": n_text,
                    "delivered_at": None,
                }
            )
        else:
            target_notices = notices_path if notices_path is not None else default_notices_path()
            notices_out.append(append_notice(target_notices, kind=n_kind, text=n_text))

    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "notices": notices_out,
    }
