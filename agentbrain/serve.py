"""服务面 CLI 入口：提供 serve（HTTP）与 mcp-stdio（MCP）子命令。"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from agentbrain.config import AppConfig, load_config
from agentbrain.mcp_server import run_mcp_stdio
from agentbrain.server import run_http_server


def register_serve_subparsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """向主 CLI 注册 serve 与 mcp-stdio 子命令。"""
    p_serve = sub.add_parser("serve", help="启动 HTTP 服务（标准库 http.server）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=None, help="监听端口（默认读取 broker.yaml）")

    p_mcp = sub.add_parser("mcp-stdio", help="启动 MCP stdio 服务")
    p_mcp.add_argument("--client-id", default="codex", help="stdio 本地调用方 client_id（默认 codex）")


def dispatch_serve_command(config: AppConfig, args: argparse.Namespace) -> int | None:
    """执行 serve / mcp-stdio 子命令；若非服务子命令则返回 None。"""
    if args.command == "serve":
        return run_http_server(config, host=args.host, port=args.port)
    if args.command == "mcp-stdio":
        return run_mcp_stdio(config, client_id=args.client_id)
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbrain.serve", description="AgentBrain HTTP / MCP server CLI")
    parser.add_argument("--config", default=None, help="指定 broker.yaml 路径")
    parser.add_argument("--profile", default="lily", help="指定记忆域 profile（默认 lily）")
    sub = parser.add_subparsers(dest="command", required=True)
    register_serve_subparsers(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(args_list)
    config = load_config(broker_path=args.config, profile=args.profile)
    rc = dispatch_serve_command(config, args)
    return rc if rc is not None else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
