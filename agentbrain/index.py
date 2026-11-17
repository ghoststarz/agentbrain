"""SQLite FTS5 增量索引模块（WAL 模式 + external content FTS5 + 事实键收敛）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbrain.atomic import atomic_write_json
from agentbrain.config import AppConfig
from agentbrain.parse import parse_note
from agentbrain.segment import segment_for_index

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    type TEXT,
    tags TEXT,
    status TEXT,
    category TEXT,
    confidence REAL,
    fact_key TEXT,
    valid_from TEXT,
    valid_to TEXT,
    episode TEXT,
    created TEXT,
    updated TEXT,
    last_activated TEXT,
    access_count INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    source TEXT,
    keys_json TEXT DEFAULT '[]',
    keys2_json TEXT DEFAULT '[]',
    trigger_order INTEGER DEFAULT 100,
    fm_json TEXT NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    seg TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    dst_title TEXT NOT NULL,
    dst_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
    anchor TEXT,
    embed INTEGER NOT NULL DEFAULT 0,
    relation TEXT NOT NULL DEFAULT 'links_to',
    context_line TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rev (
    scope TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    client TEXT,
    tool TEXT,
    scope TEXT,
    allowed INTEGER NOT NULL,
    digest TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    seg,
    content='chunks',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, seg) VALUES (new.id, new.seg);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, seg) VALUES('delete', old.id, old.seg);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, seg) VALUES('delete', old.id, old.seg);
    INSERT INTO chunks_fts(rowid, seg) VALUES (new.id, new.seg);
END;

CREATE INDEX IF NOT EXISTS idx_notes_fact_key ON notes(fact_key);
CREATE INDEX IF NOT EXISTS idx_chunks_note_id ON chunks(note_id);
CREATE INDEX IF NOT EXISTS idx_links_src_id ON links(src_id);
CREATE INDEX IF NOT EXISTS idx_links_dst_id ON links(dst_id);
"""


@dataclass(frozen=True)
class ReindexStats:
    """增量索引执行结果统计。"""

    added: int
    updated: int
    skipped: int
    deleted: int
    total_notes: int
    total_chunks: int
    total_links: int
    converged_facts: int
    revision: int


def connect_db(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接并启用 WAL 与外键约束。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_notes_columns(conn: sqlite3.Connection) -> None:
    """兼容旧库：若 notes 表缺少新字段，通过 ALTER TABLE 原位补齐而不重建库。"""
    existing_cols = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(notes)").fetchall()
    }
    migrations = (
        ("keys_json", "TEXT DEFAULT '[]'"),
        ("keys2_json", "TEXT DEFAULT '[]'"),
        ("trigger_order", "INTEGER DEFAULT 100"),
    )
    for col_name, col_def in migrations:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {col_name} {col_def}")


def init_db(config: AppConfig) -> Path:
    """初始化运行时目录与数据库表结构。"""
    config.broker.ensure_runtime_dirs()
    db_path = config.broker.db_path
    with connect_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _ensure_notes_columns(conn)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO rev (scope, revision, updated_at)
            VALUES (?, 0, ?)
            """,
            (config.memory.profile, now),
        )
        conn.commit()
    return db_path


# 永不进入检索索引的目录（相对 profile 根）。
# 30_Diary：只写不读的账本，只能经 diary_read 读取（受 gate 控制），
#           一旦被索引就会通过 search / prefetch 漏进模型上下文。
# 50_Inbox/new：已下线功能的遗留安全护栏，防止旧目录或外部误放的未审卡片参与检索与注入。
# 90_Archive：已退役的内容。保留是为了"不硬删"，但它的语义就是不该再被检索到。
NEVER_INDEX_DIRS = ("30_Diary", "50_Inbox/new", "90_Archive")


def _iter_profile_markdowns(profile_dir: Path) -> list[Path]:
    """扫描 profile 目录下的所有 Markdown 笔记（跳过隐藏/状态目录与禁索引目录）。"""
    if not profile_dir.exists():
        return []
    files: list[Path] = []
    for p in sorted(profile_dir.rglob("*.md")):
        rel_parts = p.relative_to(profile_dir).parts
        # 跳过 .state 等以点号开头的内部目录
        if any(part.startswith(".") for part in rel_parts):
            continue
        rel = "/".join(rel_parts)
        if any(rel == d or rel.startswith(d + "/") for d in NEVER_INDEX_DIRS):
            continue
        files.append(p)
    return files


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_str_or_none(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _as_float(val: Any, default: float = 1.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    if isinstance(val, bool):
        return 1 if val else 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _resolve_link_targets(conn: sqlite3.Connection) -> None:
    """根据笔记标题、H1 与别名回填 links.dst_id。"""
    rows = conn.execute("SELECT id, title, fm_json FROM notes").fetchall()
    title_to_id: dict[str, int] = {}
    for row in rows:
        nid = int(row["id"])
        title_to_id[str(row["title"]).lower()] = nid
        try:
            fm = json.loads(row["fm_json"])
        except json.JSONDecodeError:
            fm = {}
        aliases = fm.get("aliases") or []
        if isinstance(aliases, list):
            for alias in aliases:
                if alias:
                    title_to_id.setdefault(str(alias).strip().lower(), nid)

    link_rows = conn.execute("SELECT id, dst_title FROM links").fetchall()
    for lrow in link_rows:
        dst_title = str(lrow["dst_title"]).strip().lower()
        dst_id = title_to_id.get(dst_title)
        conn.execute(
            "UPDATE links SET dst_id = ? WHERE id = ?",
            (dst_id, int(lrow["id"])),
        )


def _converge_fact_keys(conn: sqlite3.Connection) -> int:
    """按 fact_key 分组收敛：同键保留 created 最新一条，其余标 superseded_by（不删文件）。"""
    rows = conn.execute(
        """
        SELECT id, title, fact_key, created, updated, fm_json
        FROM notes
        WHERE fact_key IS NOT NULL AND TRIM(fact_key) != ''
        """
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(str(r["fact_key"]).strip(), []).append(r)

    converged = 0
    for _key, items in groups.items():
        if len(items) <= 1:
            continue
        # 按 created 降序、updated 降序、id 降序选出最新一条
        sorted_items = sorted(
            items,
            key=lambda r: (
                str(r["created"] or ""),
                str(r["updated"] or ""),
                int(r["id"]),
            ),
            reverse=True,
        )
        newest = sorted_items[0]
        newest_ref = f"[[{newest['title']}]]"
        for old in sorted_items[1:]:
            fm = json.loads(old["fm_json"])
            if fm.get("superseded_by") != newest_ref:
                fm["superseded_by"] = newest_ref
                conn.execute(
                    "UPDATE notes SET fm_json = ? WHERE id = ?",
                    (json.dumps(fm, ensure_ascii=False), int(old["id"])),
                )
                converged += 1
    return converged


def reindex(config: AppConfig) -> ReindexStats:
    """对当前 profile 执行增量索引，清理已删除笔记并执行事实键收敛。"""
    db_path = init_db(config)
    profile_dir = config.profile_dir
    md_files = _iter_profile_markdowns(profile_dir)

    added = 0
    updated = 0
    skipped = 0
    deleted = 0

    with connect_db(db_path) as conn:
        existing_rows = conn.execute("SELECT id, path, mtime, hash FROM notes").fetchall()
        existing_by_path: dict[str, sqlite3.Row] = {str(r["path"]): r for r in existing_rows}
        seen_paths: set[str] = set()

        for file_path in md_files:
            rel_path = file_path.relative_to(profile_dir).as_posix()
            seen_paths.add(rel_path)
            stat = file_path.stat()
            mtime = float(stat.st_mtime)

            prev = existing_by_path.get(rel_path)
            if prev is not None and abs(float(prev["mtime"]) - mtime) < 1e-6:
                skipped += 1
                continue

            raw_text = file_path.read_text(encoding="utf-8")
            content_hash = _sha256_text(raw_text)
            if prev is not None and str(prev["hash"]) == content_hash:
                # 仅 mtime 变动但内容未变，更新 mtime 后跳过重建
                conn.execute(
                    "UPDATE notes SET mtime = ? WHERE id = ?",
                    (mtime, int(prev["id"])),
                )
                skipped += 1
                continue

            parsed = parse_note(file_path, raw_text)
            fm = parsed.frontmatter
            tags_val = fm.get("tags") or []
            tags_json = json.dumps(tags_val if isinstance(tags_val, list) else [tags_val], ensure_ascii=False)
            keys_json = json.dumps(parsed.keys, ensure_ascii=False)
            keys2_json = json.dumps(parsed.keys2, ensure_ascii=False)
            trigger_order = parsed.order
            fm_json = json.dumps(fm, ensure_ascii=False)

            if prev is None:
                cur = conn.execute(
                    """
                    INSERT INTO notes (
                        path, title, type, tags, status, category, confidence,
                        fact_key, valid_from, valid_to, episode,
                        created, updated, last_activated, access_count, pinned, source,
                        keys_json, keys2_json, trigger_order,
                        fm_json, mtime, hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rel_path,
                        parsed.title,
                        _as_str_or_none(fm.get("type")),
                        tags_json,
                        _as_str_or_none(fm.get("status")),
                        _as_str_or_none(fm.get("category")),
                        _as_float(fm.get("confidence"), 1.0),
                        _as_str_or_none(fm.get("fact_key")),
                        _as_str_or_none(fm.get("valid_from")),
                        _as_str_or_none(fm.get("valid_to")),
                        _as_str_or_none(fm.get("episode")),
                        _as_str_or_none(fm.get("created")),
                        _as_str_or_none(fm.get("updated")),
                        _as_str_or_none(fm.get("last_activated")),
                        _as_int(fm.get("access_count"), 0),
                        _as_int(fm.get("pinned"), 0),
                        _as_str_or_none(fm.get("source")),
                        keys_json,
                        keys2_json,
                        trigger_order,
                        fm_json,
                        mtime,
                        content_hash,
                    ),
                )
                note_id = int(cur.lastrowid)
                added += 1
            else:
                note_id = int(prev["id"])
                conn.execute(
                    """
                    UPDATE notes SET
                        title = ?, type = ?, tags = ?, status = ?, category = ?, confidence = ?,
                        fact_key = ?, valid_from = ?, valid_to = ?, episode = ?,
                        created = ?, updated = ?, last_activated = ?, access_count = ?,
                        pinned = ?, source = ?, keys_json = ?, keys2_json = ?, trigger_order = ?,
                        fm_json = ?, mtime = ?, hash = ?
                    WHERE id = ?
                    """,
                    (
                        parsed.title,
                        _as_str_or_none(fm.get("type")),
                        tags_json,
                        _as_str_or_none(fm.get("status")),
                        _as_str_or_none(fm.get("category")),
                        _as_float(fm.get("confidence"), 1.0),
                        _as_str_or_none(fm.get("fact_key")),
                        _as_str_or_none(fm.get("valid_from")),
                        _as_str_or_none(fm.get("valid_to")),
                        _as_str_or_none(fm.get("episode")),
                        _as_str_or_none(fm.get("created")),
                        _as_str_or_none(fm.get("updated")),
                        _as_str_or_none(fm.get("last_activated")),
                        _as_int(fm.get("access_count"), 0),
                        _as_int(fm.get("pinned"), 0),
                        _as_str_or_none(fm.get("source")),
                        keys_json,
                        keys2_json,
                        trigger_order,
                        fm_json,
                        mtime,
                        content_hash,
                        note_id,
                    ),
                )
                conn.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
                conn.execute("DELETE FROM links WHERE src_id = ?", (note_id,))
                updated += 1

            for idx, chunk_text in enumerate(parsed.chunks):
                seg = segment_for_index(chunk_text)
                conn.execute(
                    "INSERT INTO chunks (note_id, idx, text, seg) VALUES (?, ?, ?, ?)",
                    (note_id, idx, chunk_text, seg),
                )

            for link in parsed.links:
                conn.execute(
                    """
                    INSERT INTO links (src_id, dst_title, dst_id, anchor, embed, relation, context_line)
                    VALUES (?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        link.dst_title,
                        link.anchor,
                        link.embed,
                        link.relation,
                        link.context_line,
                    ),
                )

        # 清理磁盘上已删除的笔记
        for old_path, old_row in existing_by_path.items():
            if old_path not in seen_paths:
                conn.execute("DELETE FROM notes WHERE id = ?", (int(old_row["id"]),))
                deleted += 1

        _resolve_link_targets(conn)
        converged = _converge_fact_keys(conn)

        # 更新 revision
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        rev_row = conn.execute(
            "SELECT revision FROM rev WHERE scope = ?",
            (config.memory.profile,),
        ).fetchone()
        current_rev = int(rev_row["revision"]) if rev_row else 0
        new_rev = current_rev + 1 if (added or updated or deleted or current_rev == 0) else current_rev
        conn.execute(
            """
            INSERT INTO rev (scope, revision, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET revision = excluded.revision, updated_at = excluded.updated_at
            """,
            (config.memory.profile, new_rev, now),
        )

        total_notes = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        total_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        total_links = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        conn.commit()

    # 同步写入 rev_dir 状态文件
    atomic_write_json(
        config.broker.rev_dir / f"{config.memory.profile}.json",
        {"scope": config.memory.profile, "revision": new_rev, "updated_at": now},
    )

    return ReindexStats(
        added=added,
        updated=updated,
        skipped=skipped,
        deleted=deleted,
        total_notes=total_notes,
        total_chunks=total_chunks,
        total_links=total_links,
        converged_facts=converged,
        revision=new_rev,
    )
