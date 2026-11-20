"""14 天遗忘与自动归档模块（仅由夜间复盘按配置触发）。"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentbrain.config import AppConfig
from agentbrain.index import _ensure_notes_columns, connect_db, delete_notes_from_index

if TYPE_CHECKING:
    from agentbrain.proactive import ProactiveStore

ARCHIVE_VERSION = 1
_SCAN_ROOTS = ("10_Working", "20_LongTerm")


@dataclass(frozen=True)
class ArchiveCandidate:
    """满足遗忘条件的单条归档候选。"""

    rel_path: str
    src_path: Path
    mtime_us: int
    last_access_us: int
    effective_last_us: int


@dataclass(frozen=True)
class ArchiveRecord:
    """已落盘归档记录。"""

    note_path: str
    archived_path: str
    mtime_us: int
    last_access_us: int
    archived_at_utc: str
    archive_version: int = ARCHIVE_VERSION


def _load_access_map(config: AppConfig) -> dict[str, int]:
    """从索引库读取所有笔记的 last_access_us。"""
    db_path = config.broker.db_path
    if not db_path.exists():
        return {}
    try:
        with connect_db(db_path) as conn:
            _ensure_notes_columns(conn)
            rows = conn.execute("SELECT path, last_access_us FROM notes").fetchall()
            return {str(r["path"]): int(r["last_access_us"] or 0) for r in rows}
    except Exception:
        return {}


def plan_archive(
    config: AppConfig,
    now_us: int,
    *,
    days: int = 14,
) -> list[ArchiveCandidate]:
    """扫描 10_Working/ 与 20_LongTerm/ 下所有文件，筛选超过 days 天未改且未访问的候选。"""
    profile_dir = config.profile_dir
    if not profile_dir.exists():
        return []

    access_map = _load_access_map(config)
    threshold_us = int(now_us) - max(1, int(days)) * 86_400 * 1_000_000
    candidates: list[ArchiveCandidate] = []

    for sub in _SCAN_ROOTS:
        root = profile_dir / sub
        if not root.exists() or root.is_symlink():
            continue
        for file_path in sorted(root.rglob("*")):
            if file_path.is_symlink() or not file_path.is_file():
                continue
            rel_path = file_path.relative_to(profile_dir).as_posix()
            try:
                mtime_us = int(file_path.stat().st_mtime * 1_000_000)
            except OSError:
                continue
            last_access_us = int(access_map.get(rel_path, 0))
            effective_last_us = max(mtime_us, last_access_us)
            if effective_last_us < threshold_us:
                candidates.append(
                    ArchiveCandidate(
                        rel_path=rel_path,
                        src_path=file_path,
                        mtime_us=mtime_us,
                        last_access_us=last_access_us,
                        effective_last_us=effective_last_us,
                    )
                )

    candidates.sort(key=lambda c: c.rel_path)
    return candidates


def _resolve_non_clobber_dest(dest: Path, ts_suffix: str) -> Path:
    if not dest.exists():
        return dest
    candidate = dest.with_name(f"{dest.stem}-{ts_suffix}{dest.suffix}")
    seq = 1
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem}-{ts_suffix}-{seq}{dest.suffix}")
        seq += 1
    return candidate


def apply_archive(
    config: AppConfig,
    plan: list[ArchiveCandidate],
    *,
    store: ProactiveStore | None = None,
    now_utc: dt.datetime | None = None,
) -> list[ArchiveRecord]:
    """将候选文件整文件移至 90_Archive/（不覆盖），写审计并清理索引。"""
    if not plan:
        return []

    ts_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=dt.timezone.utc)
    else:
        ts_utc = ts_utc.astimezone(dt.timezone.utc)
    archived_at_utc = ts_utc.isoformat()
    ts_suffix = ts_utc.strftime("%Y%m%d%H%M%S")

    eff_store = store
    if eff_store is None:
        from agentbrain.proactive import ProactiveStore as _Store

        eff_store = _Store.from_app_config(config)

    config.broker.ensure_runtime_dirs()
    audit_file = config.broker.audit_dir / "archive.jsonl"

    records: list[ArchiveRecord] = []
    moved_paths: list[str] = []

    for cand in plan:
        if not cand.src_path.exists() or cand.src_path.is_symlink():
            continue
        raw_dest = config.profile_dir / "90_Archive" / Path(cand.rel_path)
        dest = _resolve_non_clobber_dest(raw_dest, ts_suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cand.src_path), str(dest))
        archived_rel = dest.relative_to(config.profile_dir).as_posix()

        eff_store.record_archive_log(
            note_path=cand.rel_path,
            archived_path=archived_rel,
            mtime_us=cand.mtime_us,
            last_access_us=cand.last_access_us,
            archived_at_utc=archived_at_utc,
            archive_version=ARCHIVE_VERSION,
        )
        rec = ArchiveRecord(
            note_path=cand.rel_path,
            archived_path=archived_rel,
            mtime_us=cand.mtime_us,
            last_access_us=cand.last_access_us,
            archived_at_utc=archived_at_utc,
            archive_version=ARCHIVE_VERSION,
        )
        with audit_file.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "note_path": rec.note_path,
                        "archived_path": rec.archived_path,
                        "mtime_us": rec.mtime_us,
                        "last_access_us": rec.last_access_us,
                        "archived_at_utc": rec.archived_at_utc,
                        "archive_version": rec.archive_version,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        records.append(rec)
        moved_paths.append(cand.rel_path)

    if moved_paths:
        delete_notes_from_index(config, moved_paths)

    return records
