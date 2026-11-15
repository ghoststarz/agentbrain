"""Hermes MemoryProvider 插件：obsidian-broker（全钩子 try/except + fail-open）。"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

try:
    from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
except ImportError:
    class MemoryProvider:  # type: ignore[no-redef]
        """Fallback base class when imported outside Hermes runtime."""

    class RecallStatus:  # type: ignore[no-redef]
        def __init__(self, provider_label: str, count: int, glyph: str = "🧠") -> None:
            self.provider_label = provider_label
            self.count = count
            self.glyph = glyph

    def is_trivial_prompt(text: Optional[str]) -> bool:
        return not (text or "").strip()


_STATIC_PROTOCOL_BLOCK = (
    "[SHARED_BRAIN 记忆协议]\n"
    "- 跨端共享记忆由本地 agentbrain broker 托管，按需通过 prefetch 自动注入相关事实与决策。\n"
    "- 本块为静态协议说明，不包含动态记忆或日记正文。\n"
    "- 重要事实或偏好变更请使用记忆工具沉淀；会话压缩前自动同步接力状态。"
)


def _default_broker_yaml() -> Path:
    env_override = os.environ.get("AGENTBRAIN_BROKER_CONFIG")
    if env_override:
        return Path(env_override)
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        return Path(local_app) / "agentbrain" / "broker.yaml"
    return Path.home() / "AppData" / "Local" / "agentbrain" / "broker.yaml"


def _provider_logger() -> logging.Logger:
    logger = logging.getLogger("agentbrain.obsidian_broker")
    if not logger.handlers:
        local_app = os.environ.get("LOCALAPPDATA")
        base_dir = Path(local_app) / "agentbrain" / "logs" if local_app else Path.home() / "AppData" / "Local" / "agentbrain" / "logs"
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(base_dir / "provider.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


def _parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse flat key: value YAML lines without third-party dependencies."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'").strip()
        if key:
            result[key] = val
    return result


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "1", "on"}
    return False


def _read_diary_turn_capture(memory_yaml_path: Path) -> bool:
    """Read diary.turn_capture from memory.yaml; always default to False on any error."""
    try:
        if not memory_yaml_path.exists():
            return False
        text = memory_yaml_path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore[import-untyped]

            parsed = yaml.safe_load(text) or {}
            if isinstance(parsed, dict):
                diary = parsed.get("diary")
                if isinstance(diary, dict):
                    return _parse_bool(diary.get("turn_capture", False))
            return False
        except ImportError:
            pass

        in_diary = False
        for raw_line in text.splitlines():
            code = raw_line.split("#", 1)[0].rstrip()
            if not code.strip():
                continue
            indent = len(code) - len(code.lstrip(" "))
            stripped = code.strip()
            if indent == 0:
                in_diary = stripped.startswith("diary:")
                if in_diary and "{" in stripped and "turn_capture:" in stripped:
                    after = stripped.split("turn_capture:", 1)[1].split(",", 1)[0].split("}", 1)[0]
                    return _parse_bool(after)
                continue
            if in_diary and stripped.startswith("turn_capture:"):
                val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                return _parse_bool(val)
        return False
    except Exception:
        return False


class ObsidianBrokerMemoryProvider(MemoryProvider):
    """基于本地 HTTP mem-broker 的 Hermes MemoryProvider 实现（fail-open）。"""

    def __init__(self) -> None:
        self._logger = _provider_logger()
        self._hermes_home: str = ""
        self._session_id: str = ""
        self._port: int = 18765
        self._token: str = ""
        self._base_url: str = "http://127.0.0.1:18765"
        self._last_revision: Optional[int] = None
        self._last_recall: Optional[RecallStatus] = None
        self._turn_capture: bool = False
        self._closed: bool = False

    @property
    def name(self) -> str:
        return "obsidian-broker"

    def _load_broker_settings(self) -> tuple[Path, int, str]:
        cfg_path = _default_broker_yaml()
        port = 18765
        token = os.environ.get("AGENTBRAIN_TOKEN", "").strip()
        if cfg_path.exists():
            try:
                raw = _parse_flat_yaml(cfg_path.read_text(encoding="utf-8"))
                if raw.get("port"):
                    try:
                        port = int(raw["port"])
                    except (TypeError, ValueError):
                        port = 18765
                if not token and raw.get("token"):
                    token = raw["token"].strip()
            except Exception as exc:
                self._logger.warning("failed to parse broker config: %s", exc)
        if not token and self._hermes_home:
            env_file = Path(self._hermes_home) / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("AGENTBRAIN_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        return cfg_path, port, token

    def _http_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        timeout: float = 1.5,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers: dict[str, str] = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}

    def is_available(self) -> bool:
        """只做本地检查，不发网络请求。

        这个函数会在 agent 初始化时被调用；只要里面有一次 HTTP 探测，
        每次启动都要陪上最多 1 秒的超时。服务不可用时由 prefetch 静默降级。
        """
        try:
            cfg_path, port, _ = self._load_broker_settings()
            if not cfg_path.exists():
                return False
            self._port = port
            self._base_url = f"http://127.0.0.1:{port}"
            return True
        except Exception as exc:
            self._logger.warning("is_available failed: %s", exc)
            return False

    def _load_turn_capture(self, cfg_path: Path) -> bool:
        try:
            if not cfg_path.exists():
                return False
            raw = _parse_flat_yaml(cfg_path.read_text(encoding="utf-8"))
            vault_root_raw = raw.get("vault_root")
            if not vault_root_raw:
                return False
            vault_root = Path(os.path.expandvars(os.path.expanduser(vault_root_raw)))
            profile = Path(self._hermes_home).name if self._hermes_home else "lily"
            mem_path = vault_root / "profiles" / profile / "memory.yaml"
            if not mem_path.exists() and profile != "lily":
                mem_path = vault_root / "profiles" / "lily" / "memory.yaml"
            return _read_diary_turn_capture(mem_path)
        except Exception as exc:
            self._logger.warning("failed to load diary.turn_capture: %s", exc)
            return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        try:
            self._closed = False
            self._session_id = str(session_id or "")
            self._hermes_home = str(kwargs.get("hermes_home") or "")
            cfg_path, port, token = self._load_broker_settings()
            self._port = port
            self._token = token
            self._base_url = f"http://127.0.0.1:{port}"
            self._last_revision = None
            self._last_recall = None
            self._turn_capture = self._load_turn_capture(cfg_path)
        except Exception as exc:
            self._turn_capture = False
            self._logger.warning("initialize failed: %s", exc)

    def get_config_schema(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "key": "token",
                    "description": "AgentBrain broker bearer token",
                    "secret": True,
                    "required": False,
                    "env_var": "AGENTBRAIN_TOKEN",
                }
            ]
        except Exception as exc:
            self._logger.warning("get_config_schema failed: %s", exc)
            return []

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        try:
            if values.get("token"):
                self._token = str(values["token"]).strip()
        except Exception as exc:
            self._logger.warning("save_config failed: %s", exc)

    def system_prompt_block(self) -> str:
        try:
            return _STATIC_PROTOCOL_BLOCK[:450]
        except Exception as exc:
            self._logger.warning("system_prompt_block failed: %s", exc)
            return ""

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = None
        try:
            if self._closed or is_trivial_prompt(query):
                return ""

            prev_revision = self._last_revision
            rev: Optional[int] = None

            effective_session_id = str(session_id or self._session_id or "").strip()
            search_payload: dict[str, Any] = {"query": query, "k": 5, "hops": 1}
            if effective_session_id:
                search_payload["session_id"] = effective_session_id
            # 单次往返：revision 由 /v1/search 一并返回，不再单独探活
            res = self._http_json("POST", "/v1/search", search_payload, timeout=0.9)
            raw_rev = res.get("revision")
            if isinstance(raw_rev, int):
                rev = raw_rev
                self._last_revision = rev

            hits = res.get("hits") or []
            if not hits:
                return ""
            if all(bool(hit.get("weak_evidence")) for hit in hits):
                return ""

            lines: list[str] = []
            if rev is not None and prev_revision is not None and rev != prev_revision:
                lines.append(f"[MEMORY_DELTA] revision {prev_revision} -> {rev}")
            lines.append("[MEMORY_PREFETCH]")
            used = sum(len(line) + 1 for line in lines) - 1
            count = 0
            for hit in hits:
                title = str(hit.get("title") or "")
                excerpt = str(hit.get("excerpt") or "")
                entry = f"- {title}: {excerpt}"
                if used + len(entry) + 1 > 1200:
                    break
                lines.append(entry)
                used += len(entry) + 1
                count += 1
            if count == 0:
                return ""
            self._last_recall = RecallStatus(provider_label=self.name, count=count)
            return "\n".join(lines)[:1200]
        except Exception as exc:
            self._logger.warning("prefetch failed: %s", exc)
            return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            if self._closed or not self._turn_capture:
                return
            u_snip = " ".join((user_content or "").split())[:200]
            a_snip = " ".join((assistant_content or "").split())[:200]
            if not u_snip and not a_snip:
                return
            summary = f"session={session_id or self._session_id} | U: {u_snip} | A: {a_snip}"

            def _post_bg() -> None:
                try:
                    self._http_json(
                        "POST",
                        "/v1/diary",
                        {"action": "append", "ledger": "public", "content": summary},
                        timeout=1.5,
                    )
                except Exception as inner_exc:
                    self._logger.warning("sync_turn background post failed: %s", inner_exc)

            threading.Thread(target=_post_bg, name="obsidian-broker-sync", daemon=True).start()
        except Exception as exc:
            self._logger.warning("sync_turn failed: %s", exc)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        try:
            if self._closed or action == "remove" or not (content or "").strip():
                return
            self._http_json(
                "POST",
                "/v1/write",
                {
                    "content": content,
                    "type": "fact",
                    "tags": ["hermes-mirror", str(target or "memory")],
                },
                timeout=1.5,
            )
        except Exception as exc:
            self._logger.warning("on_memory_write failed: %s", exc)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        try:
            if self._closed or not messages:
                return ""
            last_user = ""
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    last_user = " ".join(str(msg.get("content") or "").split())[:240]
                    break
            task_key = f"hermes:{self._session_id or 'default'}"
            summary = (last_user or "pre-compress checkpoint").strip()

            try:
                existing_res = self._http_json(
                    "POST",
                    "/v1/handoff",
                    {"action": "read"},
                    timeout=1.5,
                )
                tasks = existing_res.get("tasks") if isinstance(existing_res, dict) else None
                existing = tasks.get(task_key) if isinstance(tasks, dict) else None
                if isinstance(existing, dict):
                    existing_summary = str(existing.get("summary") or "").strip()
                    existing_status = str(existing.get("status") or "").strip().lower()
                    if existing_summary == summary and existing_status == "in_progress":
                        return ""
            except Exception as read_exc:
                self._logger.warning("on_pre_compress handoff read failed: %s", read_exc)

            self._http_json(
                "POST",
                "/v1/handoff",
                {
                    "action": "upsert",
                    "task_key": task_key,
                    "status": "in_progress",
                    "summary": summary,
                },
                timeout=1.5,
            )
        except Exception as exc:
            self._logger.warning("on_pre_compress failed: %s", exc)
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        try:
            self._closed = True
            self._last_recall = None
        except Exception as exc:
            self._logger.warning("shutdown failed: %s", exc)


def register(ctx: Any) -> None:
    """Hermes 插件入口：注册 ObsidianBrokerMemoryProvider 实例。"""
    try:
        ctx.register_memory_provider(ObsidianBrokerMemoryProvider())
    except Exception as exc:
        _provider_logger().warning("register failed: %s", exc)
