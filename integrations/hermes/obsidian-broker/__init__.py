"""Hermes MemoryProvider 插件：obsidian-broker（全钩子 try/except + fail-open）。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
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
    "- 重要事实或偏好变更可由后台评审自动沉淀；会话压缩前自动同步接力状态。\n"
    "- 需要细节时调用 memory_search / memory_read；不要只凭 MEMORY_INDEX 的摘要推断事实。"
)

_CATEGORY_LABELS = {
    "longterm_upsert": "长期记忆",
    "project_update": "项目页",
    "hot_update": "热记忆",
    "handoff_upsert": "接力",
    "diary_append": "日记",
}


def _publish_review_notice(session_id: str, text: str) -> bool:
    """把回执作为会话内系统提示（review.summary）推给客户端。

    与 Hermes 自身的 "💾 Memory updated" 同一个通道：只出现在聊天界面，不进模型上下文，
    也不占用模型输出。插件与网关同进程，取不到网关（CLI/旧版本）时返回 False 由调用方回退。
    """
    sid = str(session_id or "").strip()
    if not sid or not text:
        return False
    try:
        from tui_gateway import server as _gateway

        emit = getattr(_gateway, "_emit", None)
        if emit is None:
            return False
        return bool(emit("review.summary", sid, {"text": text}))
    except Exception:
        return False


def _format_review_receipts(receipts: Any) -> tuple[list[str], list[int]]:
    """格式化一次性后台记忆整理完成回执（仅含结果/分类计数，不含日记或对话正文）。"""
    if not isinstance(receipts, list) or not receipts:
        return [], []
    lines: list[str] = []
    ids: list[int] = []
    for item in receipts:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        if isinstance(rid, int) and rid > 0:
            ids.append(rid)
        job_type = str(item.get("job_type") or "incremental").strip().lower()
        scope_label = "夜间后台记忆整理" if job_type == "nightly" else "上轮后台记忆整理"
        outcome = str(item.get("outcome") or "").strip().lower()
        if outcome == "applied":
            # 只报“写了 / 没写”，不暴露任何具体内容或条目计数（分类信息仍可留在服务端审计）。
            body = f"{scope_label}已完成：长期记忆已写"
        elif outcome == "no_change":
            body = f"{scope_label}已完成：记忆no_change"
        elif outcome == "failed":
            err = str(item.get("error_summary") or "unknown_error").replace("\n", " ").strip()[:120]
            body = f"{scope_label}失败：{err}"
        else:
            continue
        # 干净的展示文案；是否要求模型转述由调用方决定（正常走界面提示，不需要模型参与）。
        lines.append(f"💾 {body}")
    return lines, ids



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


_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_INJECTED_BLOCK_RE = re.compile(
    r"(?:\[SHARED_BRAIN[^\]]*\][\s\S]*?(?=\n\n|\Z)|"
    r"\[MEMORY_DELTA\][^\n]*\n?|"
    r"\[MEMORY_PREFETCH\][\s\S]*?(?=\n\n|\Z)|"
    r"\[MEMORY_INDEX\][\s\S]*?(?=\n\n|\Z)|"
    r"\[PREVIOUS_REVIEW_RESULT\][^\n]*\n?|"
    r"\[DIARY\][^\n]*\n?)"
)

_SYSTEM_NOTICE_PREFIXES = (
    "[IMPORTANT:",
    "[INTERNAL NOTIFICATION",
    "[PREVIOUS_REVIEW_RESULT]",
    "[DIARY]",
)
_MULTI_BLANK_LINES_RE = re.compile(r"(?:\n[ \t]*){2,}")
_DEFAULT_MAX_PAYLOAD_CHARS = 16000


def _extract_user_segment(text: str) -> str:
    """从 'User: ...\\nAssistant: ...' 格式中提取首段 User 正文；若无前缀则返回原文。"""
    s = (text or "").lstrip()
    if s.startswith("User:"):
        after_user = s[len("User:") :]
        idx = after_user.find("\nAssistant:")
        if idx >= 0:
            return after_user[:idx]
        return after_user
    return text or ""


def _normalize_user_text(text: str) -> str:
    """归一化用户文本：统一换行、去首尾空白、合并连续空行。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    return _MULTI_BLANK_LINES_RE.sub("\n\n", normalized)


def classify_user_turn(text: str) -> str:
    """与 agentbrain.proactive.classify_user_turn 等价的纯标准库实现。"""
    user_part = _extract_user_segment(text)
    norm = _normalize_user_text(user_part)
    if norm.startswith(_SYSTEM_NOTICE_PREFIXES) or "heartbeat #" in norm.lower():
        return "system_notice"
    return "user"


def _read_proactive_capture_flags(memory_yaml_path: Path) -> tuple[bool, bool]:
    """Pure-stdlib parser for proactive.enabled and proactive.event_capture (no PyYAML)."""
    enabled, event_capture, _ = _read_proactive_capture_settings(memory_yaml_path)
    return enabled, event_capture


def _read_proactive_capture_settings(memory_yaml_path: Path) -> tuple[bool, bool, int]:
    """Pure-stdlib parser for proactive.enabled, event_capture, and max_payload_chars."""
    try:
        if not memory_yaml_path.exists():
            return False, False, _DEFAULT_MAX_PAYLOAD_CHARS
        text = memory_yaml_path.read_text(encoding="utf-8")
        in_proactive = False
        proactive_indent = -1
        enabled = False
        event_capture = False
        max_payload_chars = _DEFAULT_MAX_PAYLOAD_CHARS
        for raw_line in text.splitlines():
            code = raw_line.split("#", 1)[0].rstrip()
            if not code.strip():
                continue
            indent = len(code) - len(code.lstrip(" "))
            stripped = code.strip()
            if indent == 0:
                in_proactive = stripped.startswith("proactive:")
                proactive_indent = indent if in_proactive else -1
                continue
            if in_proactive:
                if indent <= proactive_indent:
                    in_proactive = False
                    continue
                if stripped.startswith("enabled:"):
                    enabled = _parse_bool(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
                elif stripped.startswith("event_capture:"):
                    event_capture = _parse_bool(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
                elif stripped.startswith("max_payload_chars:"):
                    raw_val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    try:
                        parsed = int(raw_val)
                        if parsed >= 256:
                            max_payload_chars = parsed
                    except ValueError:
                        pass
        return enabled, event_capture, max_payload_chars
    except Exception:
        return False, False, _DEFAULT_MAX_PAYLOAD_CHARS


def _strip_thinking_and_memory(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", text or "")
    cleaned = _INJECTED_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


_MIRROR_TITLE_MAX = 24


def _looks_like_system_notice(text: str) -> bool:
    """判断这条消息是宿主注入的系统通知而不是用户提问（与写入侧同一套前缀）。"""
    stripped = (text or "").lstrip()
    if not stripped:
        return True
    if stripped.startswith(_SYSTEM_NOTICE_PREFIXES):
        return True
    return "heartbeat #" in stripped.lower()[:200]


def _derive_mirror_title(text: str) -> str:
    """从记忆条目正文派生短标题（避免把整句话当文件名）。"""
    first = ""
    for line in str(text or "").splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            first = clean
            break
    if not first:
        return ""
    for sep in ("：", ":", "。", "；", ";", "，", ","):
        idx = first.find(sep)
        if 0 < idx <= _MIRROR_TITLE_MAX:
            first = first[:idx]
            break
    return " ".join(first.split())[:_MIRROR_TITLE_MAX].strip()


def _is_filtered_turn(session_id: str, turn_author: Optional[dict[str, Any]]) -> bool:
    sid = (session_id or "").strip().lower()
    if sid.startswith(("proactive-review", "system", "heartbeat", "cron", "__internal")):
        return True
    if isinstance(turn_author, dict):
        if turn_author.get("is_bot") is True or turn_author.get("is_system") is True:
            return True
        for key in ("role", "kind", "type", "name"):
            val = str(turn_author.get(key) or "").strip().lower()
            if val in {"system", "bot", "heartbeat", "proactive_review", "review", "cron"}:
                return True
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
        self._proactive_enabled: bool = False
        self._proactive_event_capture: bool = False
        self._proactive_max_payload_chars: int = _DEFAULT_MAX_PAYLOAD_CHARS
        self._outbox_lock = threading.Lock()
        self._session_turn_seq: dict[str, int] = {}
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

    def _resolve_memory_yaml_path(self, cfg_path: Path) -> Optional[Path]:
        if not cfg_path.exists():
            return None
        raw = _parse_flat_yaml(cfg_path.read_text(encoding="utf-8"))
        vault_root_raw = raw.get("vault_root")
        if not vault_root_raw:
            return None
        vault_root = Path(os.path.expandvars(os.path.expanduser(vault_root_raw)))
        profile = Path(self._hermes_home).name if self._hermes_home else "lily"
        mem_path = vault_root / "profiles" / profile / "memory.yaml"
        if not mem_path.exists() and profile != "lily":
            mem_path = vault_root / "profiles" / "lily" / "memory.yaml"
        return mem_path

    def _load_turn_capture(self, cfg_path: Path) -> bool:
        try:
            mem_path = self._resolve_memory_yaml_path(cfg_path)
            if mem_path is None:
                return False
            return _read_diary_turn_capture(mem_path)
        except Exception as exc:
            self._logger.warning("failed to load diary.turn_capture: %s", exc)
            return False

    def _load_proactive_flags(self, cfg_path: Path) -> tuple[bool, bool]:
        enabled, event_capture, _ = self._load_proactive_settings(cfg_path)
        return enabled, event_capture

    def _load_proactive_settings(self, cfg_path: Path) -> tuple[bool, bool, int]:
        try:
            mem_path = self._resolve_memory_yaml_path(cfg_path)
            if mem_path is None:
                return False, False, _DEFAULT_MAX_PAYLOAD_CHARS
            return _read_proactive_capture_settings(mem_path)
        except Exception as exc:
            self._logger.warning("failed to load proactive capture flags: %s", exc)
            return False, False, _DEFAULT_MAX_PAYLOAD_CHARS

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
            (
                self._proactive_enabled,
                self._proactive_event_capture,
                self._proactive_max_payload_chars,
            ) = self._load_proactive_settings(cfg_path)
            if self._proactive_enabled and self._proactive_event_capture and self._outbox_path().exists():
                threading.Thread(
                    target=self._flush_outbox,
                    name="obsidian-broker-proactive-init-flush",
                    daemon=True,
                ).start()
        except Exception as exc:
            self._turn_capture = False
            self._proactive_enabled = False
            self._proactive_event_capture = False
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
            if _looks_like_system_notice(query):
                # 后台任务完成通知、复盘回执这类消息不是他在提问：拿它去检索只会
                # 命中一堆无关笔记白吃注入预算（实测审计里出现过这种 query）。
                return ""

            prev_revision = self._last_revision
            rev: Optional[int] = None

            effective_session_id = str(session_id or self._session_id or "").strip()
            search_payload: dict[str, Any] = {"query": query, "k": 5, "hops": 1, "inject": True}
            if effective_session_id:
                search_payload["session_id"] = effective_session_id
            # 单次往返：revision 由 /v1/search 一并返回，不再单独探活
            res = self._http_json("POST", "/v1/search", search_payload, timeout=0.9)
            raw_rev = res.get("revision")
            if isinstance(raw_rev, int):
                rev = raw_rev
                self._last_revision = rev

            hits = res.get("hits") or []
            receipt_lines, receipt_ids = _format_review_receipts(res.get("review_receipts"))
            if receipt_lines and effective_session_id:
                # 优先走界面提示（review.summary）：不占模型上下文、不占模型输出。
                published_ids: list[int] = []
                fallback_lines: list[str] = []
                for line, rid in zip(receipt_lines, receipt_ids):
                    if _publish_review_notice(effective_session_id, line):
                        published_ids.append(rid)
                    else:
                        fallback_lines.append(
                            f"[PREVIOUS_REVIEW_RESULT] {line.lstrip('💾 ')}"
                            "（请在本轮回复开头用一行告诉用户这个结果，不要展开细节）"
                        )
                receipt_lines = fallback_lines
                if published_ids:
                    try:
                        self._http_json(
                            "POST",
                            "/v1/review/receipts/ack",
                            {"session_id": effective_session_id, "receipt_ids": published_ids},
                            timeout=0.5,
                        )
                    except Exception as ack_exc:
                        self._logger.warning("review receipts ack failed: %s", ack_exc)
            if receipt_ids and effective_session_id:
                try:
                    self._http_json(
                        "POST",
                        "/v1/review/receipts/ack",
                        {"session_id": effective_session_id, "receipt_ids": receipt_ids},
                        timeout=0.5,
                    )
                except Exception as ack_exc:
                    self._logger.warning("review receipts ack failed: %s", ack_exc)

            if not hits or all(bool(hit.get("weak_evidence")) for hit in hits):
                if receipt_lines:
                    self._last_recall = None
                    return "\n".join(receipt_lines)[:1200]
                return ""

            lines: list[str] = []
            if rev is not None and prev_revision is not None and rev != prev_revision:
                lines.append(f"[MEMORY_DELTA] revision {prev_revision} -> {rev}")
            lines.extend(receipt_lines)
            lines.append("[MEMORY_INDEX]")
            used = sum(len(line) + 1 for line in lines) - 1
            count = 0
            ordered_hits = sorted(
                hits,
                key=lambda h: (float(h.get("score") or 0.0), str(h.get("updated") or "")),
                reverse=True,
            )
            for hit in ordered_hits:
                raw_path = str(hit.get("path") or "").strip()
                if raw_path:
                    name = Path(raw_path).stem
                else:
                    raw_title = str(hit.get("title") or "").strip()
                    name = raw_title[:-3] if raw_title.lower().endswith(".md") else raw_title
                raw_summary = " ".join(str(hit.get("summary") or "").split()).strip()
                if not raw_summary:
                    raw_summary = " ".join(str(hit.get("excerpt") or "").split()).strip()[:100]
                if len(raw_summary) > 120:
                    raw_summary = raw_summary[:119].rstrip() + "…"
                entry = f"- {name} — {raw_summary}"
                if used + len(entry) + 1 > 1200:
                    break
                lines.append(entry)
                used += len(entry) + 1
                count += 1
            if count == 0:
                return "\n".join(receipt_lines)[:1200] if receipt_lines else ""
            self._last_recall = RecallStatus(provider_label=self.name, count=count)
            return "\n".join(lines)[:1200]
        except Exception as exc:
            self._logger.warning("prefetch failed: %s", exc)
            return ""

    _OUTBOX_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
        (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer [REDACTED_TOKEN]"),
        (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
        (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED_GH_TOKEN]"),
        (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GH_TOKEN]"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
        (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
        (
            re.compile(
                r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|access_token|auth_token|token)\b"
                r"(\s*[:=]\s*['\"]?)([^\s'\",;]{8,})(['\"]?)"
            ),
            r"\1\2[REDACTED]\4",
        ),
    )

    @classmethod
    def _redact_outbox_text(cls, text: str) -> str:
        if not text:
            return text
        out = text
        for pat, repl in cls._OUTBOX_REDACT_PATTERNS:
            out = pat.sub(repl, out)
        return out

    def _outbox_path(self) -> Path:
        if self._hermes_home:
            return Path(self._hermes_home) / "cache" / "proactive_outbox.jsonl"
        local_app = os.environ.get("LOCALAPPDATA")
        base = Path(local_app) / "agentbrain" / "outbox" if local_app else Path.home() / "AppData" / "Local" / "agentbrain" / "outbox"
        return base / "hermes_proactive_outbox.jsonl"

    def _enqueue_outbox(self, event: dict[str, Any]) -> None:
        safe_event = dict(event)
        if "content" in safe_event:
            safe_event["content"] = self._redact_outbox_text(str(safe_event["content"] or ""))
        if isinstance(safe_event.get("meta"), dict):
            safe_event["meta"] = {
                k: self._redact_outbox_text(v) if isinstance(v, str) else v
                for k, v in safe_event["meta"].items()
            }
        outbox = self._outbox_path()
        with self._outbox_lock:
            outbox.parent.mkdir(parents=True, exist_ok=True)
            with outbox.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(safe_event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _flush_outbox(self) -> None:
        outbox = self._outbox_path()
        with self._outbox_lock:
            if not outbox.exists():
                return
            raw_lines = [ln for ln in outbox.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            if not raw_lines:
                return
            remaining: list[str] = []
            failed_network = False
            for idx, line in enumerate(raw_lines):
                if failed_network:
                    remaining.append(line)
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self._http_json("POST", "/v1/review/events", ev, timeout=2.0)
                except urllib.error.HTTPError as http_exc:
                    if http_exc.code in (400, 413):
                        # 永久拒绝（格式错或超限），记录日志并移出队列避免阻塞后续事件
                        self._logger.warning("proactive outbox event rejected (%s): %s", http_exc.code, ev.get("event_id"))
                    else:
                        remaining.append(line)
                        failed_network = True
                except Exception as exc:
                    self._logger.warning("proactive outbox flush transient error: %s", exc)
                    remaining.append(line)
                    failed_network = True
            tmp = outbox.with_suffix(".jsonl.tmp")
            tmp.write_text(("\n".join(remaining) + "\n") if remaining else "", encoding="utf-8")
            os.replace(tmp, outbox)

    def _derive_turn_event_id(
        self,
        session_id: str,
        messages: Optional[list[dict[str, Any]]],
    ) -> str:
        prev = self._session_turn_seq.get(session_id, 0)
        msg_count = len(messages) if isinstance(messages, list) else 0
        seq = max(prev + 1, msg_count)
        self._session_turn_seq[session_id] = seq
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict) and last_msg.get("id"):
                return f"hermes:{session_id}:turn:{seq}:{last_msg['id']}"
        return f"hermes:{session_id}:turn:{seq}"

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
            if self._closed:
                return
            eff_session = str(session_id or self._session_id or "default").strip() or "default"

            # 1. 新主动评审事件采集（独立开关，默认关闭；持久 outbox + 重试）
            if (
                self._proactive_enabled
                and self._proactive_event_capture
                and not _is_filtered_turn(eff_session, turn_author)
            ):
                clean_u = _strip_thinking_and_memory(user_content)
                clean_a = _strip_thinking_and_memory(assistant_content)
                if clean_u or clean_a:
                    content_text = f"User: {clean_u}\nAssistant: {clean_a}".strip()
                    is_system_notice = (
                        classify_user_turn(user_content or "") == "system_notice"
                        or classify_user_turn(content_text) == "system_notice"
                    )
                    if is_system_notice and len(content_text) > self._proactive_max_payload_chars:
                        self._logger.warning(
                            "skip oversized system_notice turn (%d > %d chars): session=%s",
                            len(content_text),
                            self._proactive_max_payload_chars,
                            eff_session,
                        )
                    else:
                        ev_id = self._derive_turn_event_id(eff_session, messages)
                        ev_payload = {
                            "client": "hermes",
                            "session_id": eff_session,
                            "event_id": ev_id,
                            "event_kind": "reference_only" if is_system_notice else "human_turn",
                            "content": content_text,
                            "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "qualifies_nightly": True,
                        }
                        self._enqueue_outbox(ev_payload)
                        threading.Thread(
                            target=self._flush_outbox,
                            name="obsidian-broker-proactive-flush",
                            daemon=True,
                        ).start()

            # 2. 旧版 raw turn_capture（保持默认关闭，互不干扰）
            if not self._turn_capture:
                return
            u_snip = " ".join((user_content or "").split())[:200]
            a_snip = " ".join((assistant_content or "").split())[:200]
            if not u_snip and not a_snip:
                return
            summary = f"session={eff_session} | U: {u_snip} | A: {a_snip}"

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

    def on_delegation(
        self,
        task: str = "",
        result: str = "",
        *,
        child_session_id: str = "",
        session_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """子任务委派钩子：仅作为 reference_only 证据采集，不计入人类完成轮次。"""
        try:
            if self._closed or not (self._proactive_enabled and self._proactive_event_capture):
                return
            eff_session = str(session_id or self._session_id or "default").strip() or "default"
            clean_task = _strip_thinking_and_memory(task)
            clean_res = _strip_thinking_and_memory(result)
            if not clean_task and not clean_res:
                return
            meta_payload: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {"source": "delegation"}
            if child_session_id:
                meta_payload["child_session_id"] = str(child_session_id)
            ev_payload = {
                "client": "hermes",
                "session_id": eff_session,
                "event_id": f"hermes:{eff_session}:delegation:{uuid.uuid4().hex[:12]}",
                "event_kind": "reference_only",
                "content": f"[Delegation] Task: {clean_task}\nResult: {clean_res}".strip(),
                "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "meta": meta_payload,
                "qualifies_nightly": True,
            }
            self._enqueue_outbox(ev_payload)
            threading.Thread(
                target=self._flush_outbox,
                name="obsidian-broker-proactive-flush",
                daemon=True,
            ).start()
        except Exception as exc:
            self._logger.warning("on_delegation failed: %s", exc)

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
            meta = metadata if isinstance(metadata, dict) else {}
            payload: dict[str, Any] = {
                "content": content,
                "type": "fact",
                "tags": ["hermes-mirror", str(target or "memory")],
                # 镜像写入必须是 upsert：同名/同条目原地更新，不要每改一次就长出一份带时间戳的副本
                "upsert": True,
            }
            derived_title = _derive_mirror_title(content)
            if derived_title:
                payload["title"] = derived_title
            prev = str(meta.get("previous_content") or "").strip()
            if prev:
                # 这条记忆被编辑前的原文：服务端据此精确找回同一条笔记并原地覆盖
                payload["previous_content"] = prev
            self._http_json("POST", "/v1/write", payload, timeout=1.5)
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
        return [
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "按关键词检索长期记忆与工作记忆，返回匹配的笔记摘要列表。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "检索关键词或问题",
                            },
                            "k": {
                                "type": "integer",
                                "description": "返回条数上限，默认 5",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_read",
                    "description": "按标题或路径读取单篇长期记忆或工作记忆的完整正文与关联链接。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "笔记标题或文件名（不含 .md），与 path 二选一",
                            },
                            "path": {
                                "type": "string",
                                "description": "相对 profile 的笔记路径（仅限 20_LongTerm/ 或 10_Working/），与 title 二选一",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """分发 memory_search / memory_read 工具调用，一律返回 JSON 字符串，错误返回 {"error": ...}。"""
        try:
            if self._closed:
                return json.dumps({"error": "provider is closed"}, ensure_ascii=False)
            params = args if isinstance(args, dict) else {}

            if tool_name == "memory_search":
                query = str(params.get("query") or "").strip()
                if not query:
                    return json.dumps({"error": "query is required"}, ensure_ascii=False)
                raw_k = params.get("k", 5)
                try:
                    k = max(1, int(raw_k))
                except (TypeError, ValueError):
                    k = 5
                # 主动检索工具不带 inject，不走 prefetch 过滤
                res = self._http_json("POST", "/v1/search", {"query": query, "k": k}, timeout=2.0)
                raw_hits = res.get("hits") if isinstance(res, dict) else []
                hits_out: list[dict[str, Any]] = []
                for h in raw_hits or []:
                    if not isinstance(h, dict):
                        continue
                    summary = " ".join(str(h.get("summary") or "").split()).strip()
                    if not summary:
                        summary = " ".join(str(h.get("excerpt") or "").split()).strip()[:100]
                    hits_out.append(
                        {
                            "title": str(h.get("title") or ""),
                            "path": str(h.get("path") or ""),
                            "summary": summary,
                            "score": h.get("score", 0.0),
                        }
                    )
                return json.dumps({"hits": hits_out, "total": len(hits_out)}, ensure_ascii=False)

            if tool_name == "memory_read":
                title = str(params.get("title") or "").strip()
                path = str(params.get("path") or "").strip()
                if not title and not path:
                    return json.dumps({"error": "title or path is required"}, ensure_ascii=False)
                req_payload: dict[str, Any] = {}
                if title:
                    req_payload["title"] = title
                if path:
                    req_payload["path"] = path
                res = self._http_json("POST", "/v1/note", req_payload, timeout=2.0)
                out: dict[str, Any] = {
                    "title": str(res.get("title") or ""),
                    "path": str(res.get("path") or ""),
                    "summary": str(res.get("summary") or ""),
                    "content": str(res.get("content") or ""),
                    "links": res.get("links") if isinstance(res.get("links"), list) else [],
                    "updated": str(res.get("updated") or ""),
                }
                if res.get("truncated"):
                    out["truncated"] = True
                return json.dumps(out, ensure_ascii=False)

            return json.dumps({"error": f"unknown tool: {tool_name}"}, ensure_ascii=False)
        except urllib.error.HTTPError as http_exc:
            detail = ""
            try:
                body = http_exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    err_code = str(parsed.get("error") or f"http_{http_exc.code}")
                    err_detail = str(parsed.get("detail") or "").strip()
                    detail = f"{err_code}: {err_detail}" if err_detail else err_code
                else:
                    detail = body.strip() or f"http_{http_exc.code}"
            except Exception:
                detail = f"http_{http_exc.code}"
            return json.dumps({"error": detail}, ensure_ascii=False)
        except Exception as exc:
            self._logger.warning("handle_tool_call(%s) failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

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
