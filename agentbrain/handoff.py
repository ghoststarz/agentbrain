"""接力卡读写模块：结构化任务槽位与原子替换写入。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from agentbrain.acl import display_client_id
from agentbrain.atomic import atomic_write_json
from agentbrain.config import AppConfig

_CLOSED_STATUSES = frozenset({"closed", "done", "completed", "cancelled", "archived"})


def handoff_path(config: AppConfig) -> Path:
    """返回当前 profile 的 handoff.json 路径。"""
    return config.profile_dir / "10_Working" / ".state" / "handoff.json"


def _normalize_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def read_handoff(config: AppConfig) -> dict[str, Any]:
    """读取完整接力卡结构。"""
    path = handoff_path(config)
    if not path.exists():
        return {"tasks": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"handoff.json 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        return {"tasks": {}}
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    return {"tasks": tasks}


def list_open_handoffs(config: AppConfig, limit: int = 3) -> list[dict[str, Any]]:
    """返回最多 limit 条未关闭的接力任务，按 updated_at 降序。"""
    data = read_handoff(config)
    open_items: list[dict[str, Any]] = []
    for key, entry in data["tasks"].items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "open").strip().lower()
        if status in _CLOSED_STATUSES:
            continue
        item = dict(entry)
        item.setdefault("task_key", str(key))
        open_items.append(item)

    open_items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return open_items[: max(0, limit)]


def upsert_handoff(
    config: AppConfig,
    *,
    task_key: str,
    status: str,
    summary: str,
    next_steps: Any = None,
    touched_files: Any = None,
    updated_by: str = "",
) -> dict[str, Any]:
    """原子更新或插入一条接力卡任务槽位。"""
    clean_key = str(task_key or "").strip()
    if not clean_key:
        raise ValueError("task_key 不能为空")
    clean_status = str(status or "open").strip()
    clean_summary = str(summary or "").strip()

    data = read_handoff(config)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    entry = {
        "task_key": clean_key,
        "status": clean_status,
        "summary": clean_summary,
        "next_steps": _normalize_str_list(next_steps),
        "touched_files": _normalize_str_list(touched_files),
        "updated_at": now_iso,
        "updated_by": display_client_id(updated_by),
    }
    data["tasks"][clean_key] = entry
    atomic_write_json(handoff_path(config), data)
    return {
        "ok": True,
        "task_key": clean_key,
        "task": entry,
        "total_tasks": len(data["tasks"]),
    }
