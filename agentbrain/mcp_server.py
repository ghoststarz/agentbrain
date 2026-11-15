"""MCP 传输层：基于官方 mcp SDK 的 stdio server，封装在 BrokerService 之上。"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from agentbrain.acl import AuthError, AuthIdentity, authenticate_bearer
from agentbrain.config import AppConfig
from agentbrain.server import BrokerService


def _build_stdio_identity(config: AppConfig, client_id: str) -> AuthIdentity:
    client_cfg = config.memory.clients.get(client_id)
    if isinstance(client_cfg, dict) and client_cfg.get("domain"):
        return AuthIdentity(
            client_id=client_id,
            domain=str(client_cfg["domain"]),
            can_casual=bool(client_cfg.get("can_casual", False)),
        )
    return AuthIdentity(client_id=client_id, domain="work", can_casual=False)


def _resolve_stdio_identity(config: AppConfig, fallback_client_id: str) -> AuthIdentity:
    """stdio 进程的身份解析顺序：token > AGENTBRAIN_CLIENT_ID > 参数默认值。

    之前硬编码默认 'codex'，导致 agy 的调用全被记成 codex——
    日记作者错标、cooldown 键也串在一起。
    """
    raw_token = os.environ.get("AGENTBRAIN_TOKEN", "").strip()
    if raw_token:
        try:
            return authenticate_bearer(f"Bearer {raw_token}", config)
        except AuthError as exc:
            print(f"WARN: AGENTBRAIN_TOKEN 无效（{exc}），回退到 client_id={fallback_client_id}", file=sys.stderr)

    env_client = os.environ.get("AGENTBRAIN_CLIENT_ID", "").strip()
    if env_client:
        return _build_stdio_identity(config, env_client)

    return _build_stdio_identity(config, fallback_client_id)


def _to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def create_mcp_server(config: AppConfig, client_id: str = "codex") -> MCPServer:
    """构建并注册全部 10 个 AgentBrain MCP 工具的 MCPServer 实例。"""
    service = BrokerService(config)
    identity = _resolve_stdio_identity(config, client_id)
    mcp = MCPServer("agentbrain")

    _ro = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
    _rw_idem = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
    _rw_append = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

    @mcp.tool(name="memory_boot", description="Render budget-trimmed shared brain boot block", annotations=_ro)
    def memory_boot(target: str = "codex") -> str:
        return _to_json(service.boot({"target": target}))

    @mcp.tool(name="memory_delta", description="Fetch memory changes since a given revision", annotations=_ro)
    def memory_delta(since_rev: int = 0, target: str = "codex") -> str:
        return _to_json(service.delta({"since_rev": since_rev, "target": target}))

    @mcp.tool(name="memory_search", description="Search memory notes with FTS5 + 1-hop graph expansion", annotations=_ro)
    def memory_search(query: str, k: int = 5, hops: int = 1) -> str:
        return _to_json(service.search({"query": query, "k": k, "hops": hops}))

    @mcp.tool(name="memory_read", description="Read a note or specific heading block from the vault", annotations=_ro)
    def memory_read(note: str, block: str | None = None) -> str:
        return _to_json(service.read_note({"note": note, "block": block}, can_casual=identity.can_casual))

    @mcp.tool(name="memory_write", description="Atomically write a memory note and reindex", annotations=_rw_append)
    def memory_write(
        content: str,
        type: str = "fact",
        links: list[str] | None = None,
        tags: list[str] | None = None,
        supersedes: str | None = None,
        fact_key: str | None = None,
        title: str | None = None,
    ) -> str:
        return _to_json(
            service.write_note(
                {
                    "content": content,
                    "type": type,
                    "links": links or [],
                    "tags": tags or [],
                    "supersedes": supersedes,
                    "fact_key": fact_key,
                    "title": title,
                },
                identity=identity,
            )
        )

    @mcp.tool(name="handoff_read", description="Read structured handoff task slots", annotations=_ro)
    def handoff_read() -> str:
        return _to_json(service.handoff({"action": "read"}, identity=identity))

    @mcp.tool(name="handoff_upsert", description="Atomically upsert a structured handoff task slot", annotations=_rw_idem)
    def handoff_upsert(
        task_key: str,
        status: str,
        summary: str,
        next_steps: list[str] | None = None,
        touched_files: list[str] | None = None,
    ) -> str:
        return _to_json(
            service.handoff(
                {
                    "action": "upsert",
                    "task_key": task_key,
                    "status": status,
                    "summary": summary,
                    "next_steps": next_steps or [],
                    "touched_files": touched_files or [],
                },
                identity=identity,
            )
        )

    @mcp.tool(name="diary_append", description="Append an entry to public or private diary ledger", annotations=_rw_append)
    def diary_append(ledger: str, content: str) -> str:
        return _to_json(service.diary({"action": "append", "ledger": ledger, "content": content}, identity=identity))

    @mcp.tool(name="diary_read", description="Read recent days from public or private diary ledger", annotations=_ro)
    def diary_read(ledger: str = "public", recent_days: int = 3) -> str:
        return _to_json(service.diary({"action": "read", "ledger": ledger, "recent_days": recent_days}, identity=identity))

    @mcp.tool(name="memory_status", description="Inspect vault index counts, revision, and index age", annotations=_ro)
    def memory_status() -> str:
        return _to_json(service.status())

    return mcp


def run_mcp_stdio(config: AppConfig, client_id: str = "codex") -> int:
    """以 stdio 传输模式启动 MCP 服务。"""
    mcp = create_mcp_server(config, client_id=client_id)
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    return 0
