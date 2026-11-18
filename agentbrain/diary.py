"""公/私账日记追加与读取模块（支持 diary.gate 门控开关）。"""

from __future__ import annotations

import datetime as dt
import re
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


def append_diary(
    config: AppConfig,
    *,
    ledger: str,
    content: str,
    author: str = "",
    can_casual: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """向指定账本（public/private）的当日文件原子追加一条记录。"""
    clean_ledger = str(ledger or "").strip().lower()
    _check_gate(config, clean_ledger, can_casual=can_casual)

    clean_content = str(content or "").strip()
    if not clean_content:
        raise ValueError("diary content 不能为空")

    cur = now or dt.datetime.now()
    date_str = cur.strftime("%Y-%m-%d")
    time_str = cur.strftime("%H:%M:%S")

    target_dir = _ledger_dir(config, clean_ledger)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{date_str}.md"

    clean_author = normalize_diary_author(author)
    tag = f" ({clean_author})" if clean_author else ""
    line = f"- [{time_str}]{tag} {clean_content}\n"

    if target_file.exists():
        existing = target_file.read_text(encoding="utf-8")
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
