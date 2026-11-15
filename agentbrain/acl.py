"""鉴权与访问控制模块：Bearer token -> sha256 -> client_id -> domain，失败即拒。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbrain.config import AppConfig


def default_tokens_path() -> Path:
    """获取默认的 tokens.json 路径（支持 AGENTBRAIN_TOKENS_PATH 环境变量覆盖）。"""
    env_override = os.environ.get("AGENTBRAIN_TOKENS_PATH")
    if env_override:
        return Path(env_override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "agentbrain" / "tokens.json"
    return Path.home() / "AppData" / "Local" / "agentbrain" / "tokens.json"


@dataclass(frozen=True)
class AuthIdentity:
    """已鉴权调用方身份与服务端映射域。"""

    client_id: str
    domain: str
    can_casual: bool = False


class AuthError(Exception):
    """鉴权失败异常，携带 HTTP 状态码与 JSON 错误码。"""

    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def hash_token(token: str) -> str:
    """计算明文 token 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_tokens_map(path: Path) -> dict[str, str]:
    """加载并校验 tokens.json；缺失或损坏时抛出 503 AuthError。"""
    if not path.exists():
        raise AuthError(503, "auth_unavailable", f"tokens file missing: {path}")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(503, "auth_unavailable", f"tokens file unreadable or invalid: {exc}") from exc

    if not isinstance(raw, dict):
        raise AuthError(503, "auth_unavailable", "tokens file must be a JSON object")

    tokens_map: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise AuthError(503, "auth_unavailable", "tokens entries must map string hash to string client_id")
        tokens_map[k.lower()] = v
    return tokens_map


def authenticate_bearer(
    auth_header: str | None,
    config: AppConfig,
    tokens_path: Path | None = None,
) -> AuthIdentity:
    """校验 Authorization: Bearer <token> 并映射到 memory.yaml 中的 client/domain。"""
    resolved_tokens_path = tokens_path if tokens_path is not None else default_tokens_path()
    tokens_map = _load_tokens_map(resolved_tokens_path)

    if not auth_header or not isinstance(auth_header, str):
        raise AuthError(403, "unauthenticated", "missing Authorization header")

    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError(403, "unauthenticated", "invalid Authorization header format")

    token_digest = hash_token(parts[1].strip()).lower()
    matched_client: str | None = None
    for stored_digest, client_id in tokens_map.items():
        if hmac.compare_digest(token_digest, stored_digest):
            matched_client = client_id
            break

    if not matched_client:
        raise AuthError(403, "unauthenticated", "unknown token")

    client_cfg = config.memory.clients.get(matched_client)
    if not isinstance(client_cfg, dict) or not client_cfg.get("domain"):
        raise AuthError(403, "unauthenticated", f"client not configured in memory.yaml: {matched_client}")

    return AuthIdentity(
        client_id=matched_client,
        domain=str(client_cfg["domain"]),
        can_casual=bool(client_cfg.get("can_casual", False)),
    )
