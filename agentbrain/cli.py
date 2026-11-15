"""AgentBrain mem-broker 命令行接口：init / reindex / search / status / doctor。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from agentbrain.atomic import atomic_write_json
from agentbrain.config import AppConfig, load_config
from agentbrain.index import connect_db, init_db, reindex
from agentbrain.retrieve import search_notes
from agentbrain.serve import dispatch_serve_command, register_serve_subparsers


def _ensure_utf8_stdio() -> None:
    """在 Windows 下强制标准输出使用 UTF-8，避免中文输出编码异常。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cmd_init(config: AppConfig) -> int:
    db_path = init_db(config)
    print(f"Initialized agentbrain runtime at: {config.broker.index_dir}")
    print(f"Database: {db_path}")
    print(f"Vault: {config.broker.vault_root} (profile={config.memory.profile})")
    return 0


def _cmd_reindex(config: AppConfig) -> int:
    stats = reindex(config)
    print(
        f"Reindex complete: notes={stats.total_notes} "
        f"(added={stats.added}, updated={stats.updated}, skipped={stats.skipped}, deleted={stats.deleted}), "
        f"chunks={stats.total_chunks}, links={stats.total_links}, "
        f"converged_facts={stats.converged_facts}, revision={stats.revision}"
    )
    return 0


def _cmd_search(config: AppConfig, query: str) -> int:
    res = search_notes(config, query)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _cmd_status(config: AppConfig) -> int:
    db_path = config.broker.db_path
    notes_cnt = chunks_cnt = links_cnt = revision = 0
    updated_at = "N/A"
    if db_path.exists():
        with connect_db(db_path) as conn:
            notes_cnt = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
            chunks_cnt = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            links_cnt = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
            rev_row = conn.execute(
                "SELECT revision, updated_at FROM rev WHERE scope = ?",
                (config.memory.profile,),
            ).fetchone()
            if rev_row:
                revision = int(rev_row["revision"])
                updated_at = str(rev_row["updated_at"])

    in_vault = config.broker.vault_root in db_path.parents
    print(f"profile: {config.memory.profile}")
    print(f"vault_root: {config.broker.vault_root}")
    print(f"index_db: {db_path}")
    print(f"index_inside_vault: {in_vault}")
    print(f"notes: {notes_cnt}, chunks: {chunks_cnt}, links: {links_cnt}")
    print(f"revision: {revision} (updated_at={updated_at})")
    return 0


def _cmd_doctor(config: AppConfig) -> int:
    issues: list[str] = []

    print(f"[OK] broker.yaml: {config.broker_config_path}")
    print(f"[OK] memory.yaml: {config.memory_config_path} (identity.mode={config.memory.identity_mode})")

    if config.broker.vault_root.exists():
        print(f"[OK] vault_root exists: {config.broker.vault_root}")
    else:
        issues.append(f"vault_root missing: {config.broker.vault_root}")

    db_path = config.broker.db_path
    if config.broker.vault_root in db_path.parents:
        issues.append(f"index_db must not be inside vault: {db_path}")
    else:
        print(f"[OK] index_db isolated from vault: {db_path}")

    # 检查 FTS5 可用性
    try:
        tmp_conn = sqlite3.connect(":memory:")
        tmp_conn.execute("CREATE VIRTUAL TABLE _fts_check USING fts5(x)")
        tmp_conn.close()
        print("[OK] SQLite FTS5 available")
    except sqlite3.Error as exc:
        issues.append(f"SQLite FTS5 unavailable: {exc}")

    # 只读检查 SOUL.md 存在性与漂移
    soul_path = config.broker.soul_path
    if not soul_path.exists():
        issues.append(f"SOUL.md missing: {soul_path}")
        print(f"[FAIL] SOUL missing: {soul_path}")
    else:
        current_hash = _sha256_file(soul_path)
        baseline_file = config.broker.state_dir / "soul_baseline.json"
        if baseline_file.exists():
            baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
            expected_hash = baseline.get("sha256")
            if expected_hash == current_hash:
                print(f"[OK] SOUL exists, no drift (sha256={current_hash[:12]}...): {soul_path}")
            else:
                issues.append(f"SOUL drift detected: expected={expected_hash}, current={current_hash}")
                print(f"[WARN] SOUL drift detected: {soul_path}")
        else:
            config.broker.ensure_runtime_dirs()
            atomic_write_json(
                baseline_file,
                {"soul_path": str(soul_path), "sha256": current_hash},
            )
            print(f"[OK] SOUL exists, baseline recorded, no drift (sha256={current_hash[:12]}...): {soul_path}")

    if issues:
        for err in issues:
            print(f"[ERROR] {err}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbrain", description="AgentBrain mem-broker CLI")
    parser.add_argument("--config", default=None, help="指定 broker.yaml 路径")
    parser.add_argument("--profile", default="lily", help="指定记忆域 profile（默认 lily）")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化运行时目录与索引库")
    sub.add_parser("reindex", help="增量索引 Vault 笔记")
    p_search = sub.add_parser("search", help="检索记忆")
    p_search.add_argument("query", nargs="+", help="检索词")
    sub.add_parser("status", help="查看索引状态")
    sub.add_parser("doctor", help="环境与 SOUL 漂移诊断")
    p_agy = sub.add_parser("sync-agy", help="把身份与热记忆写进 agy 的全局规则文件")
    p_agy.add_argument("--target", default="antigravity", help="渲染目标（默认 antigravity）")
    register_serve_subparsers(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args_list = list(sys.argv[1:] if argv is None else argv)
    # 兼容 `python -m agentbrain cli <subcommand>` 与 `python -m agentbrain <subcommand>`
    if args_list and args_list[0] == "cli":
        args_list = args_list[1:]

    parser = build_parser()
    args = parser.parse_args(args_list)
    config = load_config(broker_path=args.config, profile=args.profile)

    if args.command == "init":
        return _cmd_init(config)
    if args.command == "reindex":
        return _cmd_reindex(config)
    if args.command == "search":
        return _cmd_search(config, " ".join(args.query))
    if args.command == "status":
        return _cmd_status(config)
    if args.command == "doctor":
        return _cmd_doctor(config)
    if args.command == "sync-agy":
        from agentbrain.agy_rules import sync_agy_rules

        res = sync_agy_rules(config, target=args.target)
        print(f"写入 {res['path']}（{res['chars']} 字，changed={res['changed']}）")
        if res["backed_up"]:
            print("检测到非本工具生成的内容，已备份为 .md.bak-agentbrain")
        return 0
    serve_rc = dispatch_serve_command(config, args)
    if serve_rc is not None:
        return serve_rc
    return 2
