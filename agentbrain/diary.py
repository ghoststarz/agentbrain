"""公/私账日记追加与读取模块（支持 diary.gate 门控开关）。"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentbrain.acl import display_client_id
from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig


class DiaryGateError(PermissionError):
    """日记门控开启时拒绝访问私账。"""


def _ledger_dir(config: AppConfig, ledger: str) -> Path:
    normalized = str(ledger or "").strip().lower()
    if normalized == "public":
        return config.profile_dir / "30_Diary" / "Public"
    if normalized == "private":
        return config.profile_dir / "30_Diary" / "Private"
    raise ValueError(f"不支持的 ledger 类型: {ledger!r}（仅支持 public / private）")


def normalize_diary_author(raw: str) -> str:
    """日记署名只允许 hermes / antigravity 两种来源。

    签名分歧（hermes:lily / proactive / agy_ide ...）会让同一份账本看起来像多人手写，
    而账本本来就是两个客户端的流水视图。未知来源统一记为 hermes（Hermes 侧的主导者）。
    """
    clean = str(raw or "").strip().lower()
    if not clean:
        return "hermes"
    if clean.startswith(("antigravity", "agy")):
        return "antigravity"
    return "hermes"


def _check_gate(config: AppConfig, ledger: str, *, can_casual: bool = False) -> None:
    if str(ledger).strip().lower() == "private" and config.memory.diary_gate and not can_casual:
        raise DiaryGateError("private diary gate is enabled")


VALID_DIARY_KINDS = frozenset({"daily", "wander"})
_DIARY_ENTRY_LINE_RE = re.compile(r"^-\s*\[(\d{2}:\d{2}:\d{2})\]\s*(?:\([^)]*\))?\s*(.*)$")
_WANDER_PREFIX_RE = re.compile(r"^(?:\[wander\]\s*)+", re.IGNORECASE)
_WANDER_ANY_RE = re.compile(r"\[wander\]", re.IGNORECASE)
_DEDUPE_STRIP_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")


def strip_wander_prefix(content: str) -> str:
    """剥离正文开头已有的 [wander] 标记（含多层），避免落盘产生重复标记。"""
    return _WANDER_PREFIX_RE.sub("", str(content or "").strip()).strip()


def _normalize_diary_for_dedupe(text: str) -> str:
    """去 [wander] 标记、空白与标点，仅保留 CJK 字符与 ASCII 字母数字（小写）。"""
    no_wander = _WANDER_ANY_RE.sub("", str(text or ""))
    return _DEDUPE_STRIP_RE.sub("", no_wander).lower()


def _cjk_bigram_jaccard(norm_a: str, norm_b: str) -> float:
    """计算规范化文本的二元组 Jaccard 相似度。"""
    if not norm_a or not norm_b:
        return 0.0
    grams_a = {norm_a[i : i + 2] for i in range(len(norm_a) - 1)} if len(norm_a) >= 2 else {norm_a}
    grams_b = {norm_b[i : i + 2] for i in range(len(norm_b) - 1)} if len(norm_b) >= 2 else {norm_b}
    union = grams_a | grams_b
    if not union:
        return 0.0
    return len(grams_a & grams_b) / len(union)


def _is_near_duplicate(
    existing_entries: Iterable[str],
    new_content: str,
    threshold: float = 0.60,
) -> bool:
    """判断新日记正文是否与已有条目近义重复（规范化后取 CJK 二元组 Jaccard 与有序子序列相似度较大值）。

    注意边界：这是字符级兜底去重，只能挡近乎照抄的重复，挡不住换措辞重述（换措辞重述的
    bigram Jaccard 常低至 0.03–0.15）。切勿把该机制当主力，也不要为挡重述而调低 0.60 阈值
    （调低会误吞不相干条目）；防换措辞重述应靠在夜间上下文注入 `<today_diary>` 让模型看见已有条目。
    """
    norm_new = _normalize_diary_for_dedupe(new_content)
    if not norm_new:
        return False
    for existing in existing_entries:
        norm_existing = _normalize_diary_for_dedupe(existing)
        if not norm_existing:
            continue
        if norm_existing == norm_new:
            return True
        sim = max(
            _cjk_bigram_jaccard(norm_existing, norm_new),
            difflib.SequenceMatcher(None, norm_existing, norm_new).ratio(),
        )
        if sim >= threshold:
            return True
    return False


def _extract_diary_entry_bodies(raw_text: str) -> list[str]:
    """从单日日记全文中提取所有条目正文（不含时间戳与署名）。"""
    bodies: list[str] = []
    for line in raw_text.splitlines():
        m = _DIARY_ENTRY_LINE_RE.match(line.strip())
        if not m:
            continue
        body = m.group(2).strip()
        if body:
            bodies.append(body)
    return bodies


def normalize_diary_kind(raw: str | None) -> str:
    """规范化日记条目类型：仅允许 daily / wander，缺省 daily。"""
    clean = str(raw or "daily").strip().lower()
    if not clean:
        return "daily"
    if clean not in VALID_DIARY_KINDS:
        raise ValueError(f"不支持的 diary kind: {raw!r}（仅支持 daily / wander）")
    return clean


def append_diary(
    config: AppConfig,
    *,
    ledger: str,
    content: str,
    author: str = "",
    kind: str = "daily",
    can_casual: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """向指定账本（public/private）的当日文件原子追加一条记录。"""
    clean_ledger = str(ledger or "").strip().lower()
    _check_gate(config, clean_ledger, can_casual=can_casual)
    clean_kind = normalize_diary_kind(kind)

    clean_content = strip_wander_prefix(content)
    if not clean_content:
        raise ValueError("diary content 不能为空")

    cur = now or dt.datetime.now()
    date_str = cur.strftime("%Y-%m-%d")
    time_str = cur.strftime("%H:%M:%S")

    target_dir = _ledger_dir(config, clean_ledger)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{date_str}.md"

    existing: str | None = None
    existing_entries: list[str] = []
    if target_file.exists():
        existing = target_file.read_text(encoding="utf-8")
        existing_entries = _extract_diary_entry_bodies(existing)

    dedupe_threshold = float(getattr(config.memory.proactive, "diary_dedupe_similarity", 0.60))
    if existing_entries and _is_near_duplicate(existing_entries, clean_content, threshold=dedupe_threshold):
        return {
            "ok": True,
            "skipped_duplicate": True,
            "ledger": clean_ledger,
            "kind": clean_kind,
            "date": date_str,
            "path": str(target_file),
        }

    max_entries = int(getattr(config.memory.proactive, "diary_max_entries_per_day", 0))
    if max_entries > 0 and len(existing_entries) >= max_entries:
        return {
            "ok": True,
            "skipped_limit": True,
            "ledger": clean_ledger,
            "kind": clean_kind,
            "date": date_str,
            "path": str(target_file),
        }

    clean_author = normalize_diary_author(author)
    tag = f" ({clean_author})" if clean_author else ""
    kind_tag = " [wander]" if clean_kind == "wander" else ""
    line = f"- [{time_str}]{tag}{kind_tag} {clean_content}\n"

    if existing is not None:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_text = existing + line
    else:
        header = f"# {date_str} ({clean_ledger})\n\n"
        new_text = header + line

    atomic_write_text(target_file, new_text)
    return {
        "ok": True,
        "ledger": clean_ledger,
        "kind": clean_kind,
        "date": date_str,
        "path": str(target_file),
    }


def today_diary_status(config: AppConfig, ledger: str = "public") -> dict[str, Any]:
    """返回今日某账本的条数与最后一条的作者/时间——只给元信息，绝不给正文。

    用途：让客户端每轮都能看见「今天这栏我还没写」，把记日记从「临场想起来」
    变成「机制里一直在提示」。正文永远不进上下文，日记仍排除在检索索引之外。
    """
    clean_ledger = str(ledger or "").strip().lower()
    target_dir = _ledger_dir(config, clean_ledger)
    today = dt.date.today().isoformat()
    today_file = target_dir / f"{today}.md"

    status: dict[str, Any] = {
        "ledger": clean_ledger,
        "date": today,
        "count": 0,
        "last_time": "",
        "last_author": "",
    }
    if not today_file.exists():
        return status

    try:
        raw = today_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return status

    count = 0
    last_time = ""
    last_author = ""
    # 行格式：- [HH:MM:SS] (client_id) 正文
    line_re = re.compile(r"^-\s*\[(\d{2}:\d{2}:\d{2})\]\s*(?:\(([^)]*)\))?")
    for line in raw.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        count += 1
        last_time = m.group(1)
        last_author = display_client_id(m.group(2) or "")

    status.update({"count": count, "last_time": last_time, "last_author": last_author})
    return status


def read_diary(
    config: AppConfig,
    *,
    ledger: str,
    recent_days: int = 3,
    can_casual: bool = False,
) -> dict[str, Any]:
    """读取指定账本最近 N 天的日记文件。"""
    clean_ledger = str(ledger or "").strip().lower()
    _check_gate(config, clean_ledger, can_casual=can_casual)

    days = max(1, min(30, int(recent_days)))
    target_dir = _ledger_dir(config, clean_ledger)
    if not target_dir.exists():
        return {"ledger": clean_ledger, "gate": config.memory.diary_gate, "entries": []}

    md_files = sorted(target_dir.glob("*.md"), key=lambda p: p.stem, reverse=True)[:days]
    entries: list[dict[str, str]] = []
    for f in md_files:
        entries.append(
            {
                "date": f.stem,
                "path": str(f),
                "content": f.read_text(encoding="utf-8"),
            }
        )
    return {
        "ledger": clean_ledger,
        "gate": config.memory.diary_gate,
        "entries": entries,
    }


def collect_recent_diary_context(
    config: AppConfig,
    *,
    exclude_date: str | dt.date | None = None,
    days: int = 3,
    max_chars: int = 4000,
) -> str:
    """收集最近 N 天（不含 exclude_date 当天）的公/私账条目，按日期升序压成单行列表。

    单行格式：`YYYY-MM-DD 公|私 [hh:mm:ss] 正文`。
    总字符数受 max_chars 限制；超出时从最旧的条目开始丢弃。
    """
    eff_days = int(days)
    eff_max = int(max_chars)
    if eff_days <= 0 or eff_max <= 0:
        return ""

    if isinstance(exclude_date, dt.date):
        ref_date = exclude_date
    elif isinstance(exclude_date, str) and exclude_date.strip():
        try:
            ref_date = dt.date.fromisoformat(exclude_date.strip()[:10])
        except ValueError:
            ref_date = dt.date.today()
    else:
        ref_date = dt.date.today()

    min_date = ref_date - dt.timedelta(days=eff_days)
    # (date_str, time_str, ledger_order, line_idx, formatted_line)
    records: list[tuple[str, str, int, int, str]] = []
    for ledger, label, order in (("public", "公", 0), ("private", "私", 1)):
        target_dir = _ledger_dir(config, ledger)
        if not target_dir.exists() or target_dir.is_symlink():
            continue
        for md in sorted(target_dir.glob("*.md")):
            if md.is_symlink():
                continue
            try:
                file_date = dt.date.fromisoformat(md.stem)
            except ValueError:
                continue
            if not (min_date <= file_date < ref_date):
                continue
            try:
                raw = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for idx, raw_line in enumerate(raw.splitlines()):
                m = _DIARY_ENTRY_LINE_RE.match(raw_line.strip())
                if not m:
                    continue
                time_str = m.group(1)
                body = " ".join(m.group(2).split()).strip()
                if not body:
                    continue
                formatted = f"{md.stem} {label} [{time_str}] {body}"
                records.append((md.stem, time_str, order, idx, formatted))

    if not records:
        return ""

    records.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    lines = [r[4] for r in records]

    # 超出预算时从最旧的开始丢弃，尽量保留最新条目
    while len(lines) > 1 and sum(len(ln) for ln in lines) + (len(lines) - 1) > eff_max:
        lines.pop(0)
    joined = "\n".join(lines)
    if len(joined) > eff_max:
        joined = joined[:eff_max]
    return joined


def _resolve_window_date_str(window_id: str | dt.date | None) -> str:
    """将窗口日期规范化为 YYYY-MM-DD；非法或为空时返回空串。"""
    if isinstance(window_id, dt.date):
        return window_id.isoformat()
    if isinstance(window_id, str) and window_id.strip():
        try:
            return dt.date.fromisoformat(window_id.strip()[:10]).isoformat()
        except ValueError:
            return ""
    return ""


def count_diary_entries_for_date(
    config: AppConfig,
    *,
    ledger: str,
    window_id: str | dt.date | None,
) -> int:
    """直接读取指定日期的日记文件并统计有效条目数（不读索引）。"""
    date_str = _resolve_window_date_str(window_id)
    if not date_str:
        return 0
    target_dir = _ledger_dir(config, ledger)
    if not target_dir.exists() or target_dir.is_symlink():
        return 0
    md = target_dir / f"{date_str}.md"
    if not md.exists() or md.is_symlink():
        return 0
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(_extract_diary_entry_bodies(raw))


def _truncate_lines_to_budget(lines: list[str], max_chars: int, *, omit_idx: int | None = None) -> str:
    """在尽量保留所有行（含省略提示行）的前提下，将多行文本压缩到 max_chars 以内。"""
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined

    newline_chars = max(0, len(lines) - 1)
    omit_len = len(lines[omit_idx]) if omit_idx is not None else 0
    entry_indices = [i for i in range(len(lines)) if i != omit_idx]
    avail = max_chars - newline_chars - omit_len
    if not entry_indices or avail < len(entry_indices) * 4:
        return joined[:max_chars]

    # 按剩余预算均分给需要截断的条目行，短行不浪费配额
    lengths = {i: len(lines[i]) for i in entry_indices}
    capped: dict[int, int] = {}
    remaining_indices = list(entry_indices)
    remaining_budget = avail
    while remaining_indices:
        share = remaining_budget // len(remaining_indices)
        short = [i for i in remaining_indices if lengths[i] <= share]
        if not short:
            for idx_pos, i in enumerate(remaining_indices):
                extra = 1 if idx_pos < (remaining_budget % len(remaining_indices)) else 0
                capped[i] = max(1, share + extra)
            break
        for i in short:
            capped[i] = lengths[i]
            remaining_budget -= lengths[i]
        remaining_indices = [i for i in remaining_indices if i not in capped]

    out_lines: list[str] = []
    for i, ln in enumerate(lines):
        if i == omit_idx:
            out_lines.append(ln)
            continue
        lim = capped.get(i, len(ln))
        if len(ln) <= lim:
            out_lines.append(ln)
        elif lim >= 2:
            out_lines.append(ln[: lim - 1] + "…")
        else:
            out_lines.append(ln[:lim])
    result = "\n".join(out_lines)
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


def collect_today_diary_entries(
    config: AppConfig,
    *,
    window_id: str | dt.date | None,
    max_chars: int = 3000,
) -> str:
    """收集当天（window_id）已写下的公/私账条目，按时间升序输出 `[hh:mm:ss] 公|私 <正文>`。

    - 当天公/私账文件均不存在或无条目时返回空串。
    - 总长超出 max_chars 时，优先保留最早 2 条 + 最近 2 条，中间插入 `…（省略 N 条）`；
      若仍超限则按预算截断条目行，保证输出总长不超过 max_chars。
    """
    eff_max = int(max_chars)
    date_str = _resolve_window_date_str(window_id)
    if eff_max <= 0 or not date_str:
        return ""

    # (time_str, ledger_order, line_idx, formatted_line)
    records: list[tuple[str, int, int, str]] = []
    for ledger, label, order in (("public", "公", 0), ("private", "私", 1)):
        target_dir = _ledger_dir(config, ledger)
        if not target_dir.exists() or target_dir.is_symlink():
            continue
        md = target_dir / f"{date_str}.md"
        if not md.exists() or md.is_symlink():
            continue
        try:
            raw = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, raw_line in enumerate(raw.splitlines()):
            m = _DIARY_ENTRY_LINE_RE.match(raw_line.strip())
            if not m:
                continue
            time_str = m.group(1)
            body = " ".join(m.group(2).split()).strip()
            if not body:
                continue
            records.append((time_str, order, idx, f"[{time_str}] {label} {body}"))

    if not records:
        return ""

    records.sort(key=lambda r: (r[0], r[1], r[2]))
    lines = [r[3] for r in records]
    joined = "\n".join(lines)
    if len(joined) <= eff_max:
        return joined

    if len(lines) > 4:
        omitted = len(lines) - 4
        kept = lines[:2] + [f"…（省略 {omitted} 条）"] + lines[-2:]
        return _truncate_lines_to_budget(kept, eff_max, omit_idx=2)

    return _truncate_lines_to_budget(lines, eff_max)
