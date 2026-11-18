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
    # 手改结构化文件（HOT.md / 项目页）后只能靠 reindex 生效——这也是「内容变了」的
    # 确切信号之一，所以顺手把 agy 的规则文件刷到最新，别让 agy 继续读旧快照。
    try:
        from agentbrain.agy_rules import sync_agy_rules

        res = sync_agy_rules(config)
        if res.get("changed"):
            print(f"agy 规则文件已刷新：{res['path']} ({res['chars']} 字)")
    except Exception as exc:  # noqa: BLE001 —— 刷不了也不该让索引失败
        print(f"WARN: agy 规则同步失败（忽略）：{exc}")
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

    sub.add_parser("review-status", help="查看主动评审调度脱敏状态（不含原文）")
    sub.add_parser("review-tick", help="执行一次夜间窗口检查与可选 IDE 增量扫描")
    sub.add_parser("review-catchup", help="显式从头补录配置中的 agy_ide_roots 历史")

    p_rrun = sub.add_parser("review-run", help="运行一次主动评审 worker（支持 dry-run 与导出提示词）")
    p_rrun.add_argument("--dry-run", action="store_true", help="试运行：校验但不写入 Vault")
    p_rrun.add_argument("--export-prompts", default="", help="导出待评审任务提示词到此目录（不调用模型）")

    p_rcli = sub.add_parser("review-cli-event", help="显式记录 agy CLI 参考事件（不计入人类轮次）")
    p_rcli.add_argument("--session-id", required=True, help="会话 ID")
    p_rcli.add_argument("--content", default="", help="事件文本内容")
    p_rcli.add_argument("--file", default="", help="从文件读取事件内容")
    p_rcli.add_argument("--event-id", default="", help="可选显式 event_id")

    p_rrec = sub.add_parser("review-reconcile-countable", help="一次性幂等收敛历史可计数口径与轮次水位")
    p_rrec.add_argument("--force", action="store_true", help="强制重新扫描并收敛（忽略已迁移标记）")

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
    if args.command == "review-status":
        from agentbrain.proactive import ProactiveStore

        store = ProactiveStore.from_app_config(config)
        st = store.get_status()
        st["adapters"] = store.list_adapter_cursors()
        st["runner"] = store.get_adapter_cursor("proactive_runner", "status") or {
            "last_error": None,
            "unavailable_sources": [],
        }
        st["enabled"] = bool(config.memory.proactive.enabled)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-tick":
        if not config.memory.proactive.enabled:
            print(json.dumps({"enabled": False, "skipped": True}, ensure_ascii=False, indent=2))
            return 0
        from agentbrain.proactive import ProactiveStore
        from agentbrain.proactive_adapters import scan_agy_ide_roots

        store = ProactiveStore.from_app_config(config)
        ide = None
        if config.memory.proactive.agy_ide_enabled and config.memory.proactive.agy_ide_roots:
            ide = scan_agy_ide_roots(store, config.memory.proactive.agy_ide_roots, catchup=False)
        tick = store.tick()
        print(json.dumps({
            "tick": tick.__dict__,
            "ide": ide.__dict__ if ide else None,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-catchup":
        if not config.memory.proactive.enabled:
            print(json.dumps({"error": "proactive_disabled", "detail": "proactive.enabled 为 false"}, ensure_ascii=False))
            return 1
        from agentbrain.proactive import ProactiveStore
        from agentbrain.proactive_adapters import scan_agy_ide_roots

        store = ProactiveStore.from_app_config(config)
        ide = scan_agy_ide_roots(store, config.memory.proactive.agy_ide_roots, catchup=True)
        tick = store.tick(include_historical=True)
        print(json.dumps({
            "catchup": True,
            "ide": ide.__dict__,
            "tick": tick.__dict__,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-run":
        from agentbrain.proactive import ProactiveStore
        from agentbrain.proactive_runner import export_review_prompts
        from agentbrain.proactive_worker import run_worker_once

        store = ProactiveStore.from_app_config(config)
        if args.export_prompts:
            files = export_review_prompts(config, store, Path(args.export_prompts))
            print(json.dumps({"exported": [str(f) for f in files]}, ensure_ascii=False, indent=2))
            return 0
        if not config.memory.proactive.enabled:
            print("proactive.enabled 为 false，已拒绝运行模型评审（无网络调用）。")
            return 1
        res = run_worker_once(config, store, dry_run=bool(args.dry_run))
        print(json.dumps(res.__dict__ if res else {"status": "idle"}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-cli-event":
        if not config.memory.proactive.enabled:
            print(json.dumps({"error": "proactive_disabled", "detail": "proactive.enabled 为 false"}, ensure_ascii=False))
            return 1
        from agentbrain.proactive import ProactiveStore
        from agentbrain.proactive_adapters import ingest_cli_reference_event

        content = args.content
        if args.file:
            content = Path(args.file).read_text(encoding="utf-8")
        store = ProactiveStore.from_app_config(config)
        res = ingest_cli_reference_event(
            store,
            session_id=args.session_id,
            content=content,
            event_id=args.event_id or None,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-reconcile-countable":
        from agentbrain.proactive import ProactiveStore

        store = ProactiveStore.from_app_config(config)
        res = store.reconcile_countable_events(force=bool(args.force))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    serve_rc = dispatch_serve_command(config, args)
    if serve_rc is not None:
        return serve_rc
    return 2
