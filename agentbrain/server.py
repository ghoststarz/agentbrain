"""HTTP 服务与核心业务服务层（基于标准库 http.server，全 JSON 响应）。"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentbrain.acl import AuthError, AuthIdentity, authenticate_bearer
from agentbrain.atomic import atomic_write_text
from agentbrain.config import AppConfig
from agentbrain.diary import DiaryGateError, append_diary, read_diary
from agentbrain.handoff import read_handoff, upsert_handoff
from agentbrain.index import connect_db, reindex
from agentbrain.parse import normalize_trigger_order, parse_note
from agentbrain.render import _extract_section, get_current_revision, render_boot, render_delta
from agentbrain.retrieve import search_notes

_TYPE_DIR_MAP: dict[str, str] = {
    "fact": "20_LongTerm/Facts",
    "decision": "20_LongTerm/Decisions",
    "entity": "20_LongTerm/Entities",
    "project": "20_LongTerm/Projects",
    "moc": "20_LongTerm/MOC",
    "inbox": "50_Inbox/new",
}

_SAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_TITLE_ID_ANCHOR_RE = re.compile(r"[A-Za-z]+[-_]?\d+(?:[-_]\d+)*")


def _parse_str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if x is not None and str(x).strip()]
    else:
        items = []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _extract_hard_anchor_keys(
    title: str,
    fact_key: str | None,
    aliases: list[str],
) -> list[str]:
    """未传 keys 时的确定性兜底：仅提取 fact_key 对象名、标题编号型标识符、显式 aliases。"""
    candidates: list[str] = []
    if fact_key:
        obj_name = str(fact_key).strip().split(".")[0].strip()
        if obj_name:
            candidates.append(obj_name)
    for match in _TITLE_ID_ANCHOR_RE.finditer(title):
        candidates.append(match.group(0))
    candidates.extend(aliases)

    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _sanitize_filename(name: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("-", name).strip().strip(".")
    return cleaned[:80] if cleaned else "note"


class ServiceError(Exception):
    """业务逻辑错误，携带 HTTP 状态码与 JSON 错误信息。"""

    def __init__(self, status_code: int, error: str, detail: str = "") -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


class BrokerService:
    """共享的记忆 Broker 服务层，供 HTTP Server 与 MCP Server 复用。"""

    def __init__(self, config: AppConfig, tokens_path: Path | None = None) -> None:
        self.config = config
        self.tokens_path = tokens_path
        self._write_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._cooldowns: dict[tuple[str, str], dict[str, int]] = {}
        self._default_cooldown_turns: int = 5

    def healthz(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile": self.config.memory.profile,
            "revision": get_current_revision(self.config),
        }

    def authenticate(self, auth_header: str | None) -> AuthIdentity:
        return authenticate_bearer(auth_header, self.config, tokens_path=self.tokens_path)

    def boot(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = str(payload.get("target") or "codex")
        return render_boot(self.config, target=target)

    def delta(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            since_rev = int(payload.get("since_rev", 0))
        except (TypeError, ValueError) as exc:
            raise ServiceError(400, "invalid_request", f"since_rev must be integer: {exc}") from exc
        target = str(payload.get("target") or "codex")
        return render_delta(self.config, since_rev=since_rev, target=target)

    def search(
        self,
        payload: dict[str, Any],
        *,
        client_id: str = "default",
    ) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ServiceError(400, "invalid_request", "query is required")
        k_val = payload.get("k")
        hops_val = payload.get("hops")
        k_override = int(k_val) if k_val is not None else None
        hops_override = int(hops_val) if hops_val is not None else None
        session_id = str(payload.get("session_id") or "").strip()

        if not session_id:
            result = search_notes(
                self.config,
                query,
                k_override=k_override,
                hops_override=hops_override,
            )
            result["revision"] = get_current_revision(self.config)
            return result

        cd_key = (client_id, session_id)
        with self._cooldown_lock:
            session_cd = self._cooldowns.setdefault(cd_key, {})
            expired = [p for p, rem in session_cd.items() if rem - 1 <= 0]
            for p in expired:
                del session_cd[p]
            for p in list(session_cd):
                session_cd[p] -= 1
            blocked = set(session_cd.keys())

        result = search_notes(
            self.config,
            query,
            k_override=k_override,
            hops_override=hops_override,
            cooldown_blocked=blocked,
        )

        with self._cooldown_lock:
            session_cd = self._cooldowns.setdefault(cd_key, {})
            for hit in result.get("hits", []):
                hit_path = str(hit.get("path") or "")
                if hit_path:
                    session_cd[hit_path] = self._default_cooldown_turns

        result["revision"] = get_current_revision(self.config)
        return result

    def read_note(
        self,
        payload: dict[str, Any],
        *,
        can_casual: bool = False,
    ) -> dict[str, Any]:
        note_ref = str(payload.get("note") or "").strip()
        if not note_ref:
            raise ServiceError(400, "invalid_request", "note is required")
        block = payload.get("block")
        block_name = str(block).strip() if block is not None and str(block).strip() else None

        profile_root = self.config.profile_dir.resolve()
        candidate = (profile_root / note_ref).resolve()

        # 防止路径穿越出 profile_dir
        if profile_root not in candidate.parents and candidate != profile_root:
            raise ServiceError(403, "forbidden", "path outside profile directory")

        if not candidate.exists() or not candidate.is_file():
            # 回退按标题或文件名在索引库中查找
            resolved_rel = self._lookup_note_relpath(note_ref)
            if resolved_rel is None:
                raise ServiceError(404, "not_found", f"note not found: {note_ref}")
            candidate = (profile_root / resolved_rel).resolve()
            if not candidate.exists():
                raise ServiceError(404, "not_found", f"note file missing: {resolved_rel}")

        rel_path = candidate.relative_to(profile_root).as_posix()
        if rel_path.lower().startswith("30_diary/private/") and self.config.memory.diary_gate and not can_casual:
            raise ServiceError(403, "forbidden", "private diary gate is enabled")

        raw = candidate.read_text(encoding="utf-8")
        parsed = parse_note(candidate, raw)
        content = raw
        if block_name:
            extracted = _extract_section(parsed.body, rf"^#{{1,6}}\s+.*{re.escape(block_name)}")
            content = extracted if extracted else ""

        return {
            "path": rel_path,
            "title": parsed.title,
            "block": block_name,
            "content": content,
        }

    def _lookup_note_relpath(self, note_ref: str) -> str | None:
        db_path = self.config.broker.db_path
        if not db_path.exists():
            return None
        stem = note_ref[:-3] if note_ref.lower().endswith(".md") else note_ref
        with connect_db(db_path) as conn:
            row = conn.execute(
                "SELECT path FROM notes WHERE path = ? OR title = ? LIMIT 1",
                (note_ref.replace("\\", "/"), stem),
            ).fetchone()
            return str(row["path"]) if row else None

    def write_note(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ServiceError(400, "invalid_request", "content is required")

        note_type = str(payload.get("type") or "fact").strip().lower()
        links_raw = payload.get("links") or []
        if isinstance(links_raw, str):
            links = [x.strip() for x in links_raw.split(",") if x.strip()]
        elif isinstance(links_raw, list):
            links = [str(x).strip() for x in links_raw if str(x).strip()]
        else:
            links = []

        tags_raw = payload.get("tags") or []
        if isinstance(tags_raw, str):
            tags = [x.strip() for x in tags_raw.split(",") if x.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(x).strip() for x in tags_raw if str(x).strip()]
        else:
            tags = []

        supersedes = payload.get("supersedes")
        fact_key = payload.get("fact_key")
        title_raw = str(payload.get("title") or "").strip()
        aliases = _parse_str_list(payload.get("aliases"))
        keys = (
            _parse_str_list(payload.get("keys"))
            if "keys" in payload and payload.get("keys") is not None
            else _extract_hard_anchor_keys(
                title_raw or (content.splitlines()[0].lstrip("#").strip()[:60]),
                str(fact_key) if fact_key else None,
                aliases,
            )
        )
        keys2 = _parse_str_list(payload.get("keys2"))
        order = normalize_trigger_order(payload.get("order"), default=100)

        if not title_raw:
            first_line = content.splitlines()[0].lstrip("#").strip()
            title_raw = first_line[:60] if first_line else f"{note_type}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        today_str = dt.date.today().isoformat()
        target_subdir = _TYPE_DIR_MAP.get(note_type, "50_Inbox/new")
        target_dir = self.config.profile_dir / target_subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_stem = _sanitize_filename(title_raw)
        target_file = target_dir / f"{safe_stem}.md"
        if target_file.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            target_file = target_dir / f"{safe_stem}-{stamp}.md"

        fm_lines = [
            "---",
            f"type: {note_type}",
            f"created: {today_str}",
            f"updated: {today_str}",
            f"source: {identity.client_id}",
            f"domain: {identity.domain}",
            f"tags: {json.dumps(tags, ensure_ascii=False)}",
            "status: active",
            f"supersedes: {json.dumps(supersedes, ensure_ascii=False) if supersedes else 'null'}",
            f"fact_key: {json.dumps(fact_key, ensure_ascii=False) if fact_key else 'null'}",
            f"keys: {json.dumps(keys, ensure_ascii=False)}",
            f"keys2: {json.dumps(keys2, ensure_ascii=False)}",
            f"order: {order}",
        ]
        if aliases:
            fm_lines.append(f"aliases: {json.dumps(aliases, ensure_ascii=False)}")
        fm_lines.append("---")
        body_lines = [f"# {title_raw}", "", content]
        if links:
            body_lines.append("")
            body_lines.append("## 关联")
            for link in links:
                clean_link = link.strip("[]")
                body_lines.append(f"- related_to [[{clean_link}]]")

        full_text = "\n".join(fm_lines + body_lines) + "\n"

        with self._write_lock:
            atomic_write_text(target_file, full_text)
            stats = reindex(self.config)

        rel_path = target_file.relative_to(self.config.profile_dir).as_posix()
        parsed = parse_note(target_file, full_text)
        has_links = bool(parsed.links)

        suggested_links: list[str] = []
        if not has_links:
            search_res = search_notes(self.config, title_raw + " " + content[:80], k_override=4)
            for hit in search_res.get("hits", []):
                hit_title = str(hit.get("title") or "")
                if hit_title and hit_title != title_raw and hit_title not in suggested_links:
                    suggested_links.append(hit_title)
                if len(suggested_links) >= 3:
                    break

        return {
            "ok": True,
            "path": rel_path,
            "title": title_raw,
            "revision": stats.revision,
            "orphan_warning": not has_links,
            "suggested_links": suggested_links,
        }

    def handoff(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "read").strip().lower()
        if action == "read":
            return read_handoff(self.config)
        if action == "upsert":
            try:
                with self._write_lock:
                    return upsert_handoff(
                        self.config,
                        task_key=str(payload.get("task_key") or ""),
                        status=str(payload.get("status") or "open"),
                        summary=str(payload.get("summary") or ""),
                        next_steps=payload.get("next_steps"),
                        touched_files=payload.get("touched_files"),
                        updated_by=identity.client_id,
                    )
            except ValueError as exc:
                raise ServiceError(400, "invalid_request", str(exc)) from exc
        raise ServiceError(400, "invalid_request", f"unsupported handoff action: {action}")

    def diary(
        self,
        payload: dict[str, Any],
        *,
        identity: AuthIdentity,
    ) -> dict[str, Any]:
        action = str(payload.get("action") or "read").strip().lower()
        ledger = str(payload.get("ledger") or "public").strip().lower()
        try:
            if action == "append":
                with self._write_lock:
                    return append_diary(
                        self.config,
                        ledger=ledger,
                        content=str(payload.get("content") or ""),
                        author=identity.client_id,
                        can_casual=identity.can_casual,
                    )
            if action == "read":
                recent_days = int(payload.get("recent_days", 3))
                return read_diary(
                    self.config,
                    ledger=ledger,
                    recent_days=recent_days,
                    can_casual=identity.can_casual,
                )
        except DiaryGateError as exc:
            raise ServiceError(403, "forbidden", str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise ServiceError(400, "invalid_request", str(exc)) from exc
        raise ServiceError(400, "invalid_request", f"unsupported diary action: {action}")

    def status(self) -> dict[str, Any]:
        db_path = self.config.broker.db_path
        notes_cnt = chunks_cnt = links_cnt = revision = 0
        updated_at: str | None = None
        index_age_seconds: int | None = None

        if db_path.exists():
            with connect_db(db_path) as conn:
                notes_cnt = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
                chunks_cnt = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                links_cnt = int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
                rev_row = conn.execute(
                    "SELECT revision, updated_at FROM rev WHERE scope = ?",
                    (self.config.memory.profile,),
                ).fetchone()
                if rev_row:
                    revision = int(rev_row["revision"])
                    updated_at = str(rev_row["updated_at"])
                    try:
                        ts = dt.datetime.fromisoformat(updated_at)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt.timezone.utc)
                        index_age_seconds = max(
                            0,
                            int((dt.datetime.now(dt.timezone.utc) - ts).total_seconds()),
                        )
                    except ValueError:
                        pass

        return {
            "ok": True,
            "profile": self.config.memory.profile,
            "notes": notes_cnt,
            "chunks": chunks_cnt,
            "links": links_cnt,
            "revision": revision,
            "updated_at": updated_at,
            "index_age_seconds": index_age_seconds,
        }


class _BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        service: BrokerService,
    ) -> None:
        self.service = service
        super().__init__(server_address, handler_cls)


class BrokerHTTPHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：所有错误与响应一律返回 UTF-8 JSON。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """覆盖标准库默认的 HTML 错误页，确保任何 HTTP 错误都返回 JSON。"""
        self._send_json(code, {"error": message or "http_error", "detail": explain or ""})

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ServiceError(400, "invalid_request", "invalid Content-Length") from exc
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ServiceError(413, "payload_too_large", "request body exceeds 2MB")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(400, "invalid_json", f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ServiceError(400, "invalid_json", "JSON body must be an object")
        return data

    def _handle_request(self, method: str) -> None:
        svc: BrokerService = self.server.service  # type: ignore[attr-defined]
        path = urlparse(self.path).path

        try:
            if method == "GET" and path == "/healthz":
                self._send_json(200, svc.healthz())
                return

            identity = svc.authenticate(self.headers.get("Authorization"))

            if method == "GET" and path == "/v1/status":
                self._send_json(200, svc.status())
                return

            if method == "POST":
                payload = self._read_json_body()
                if path == "/v1/boot":
                    self._send_json(200, svc.boot(payload))
                    return
                if path == "/v1/delta":
                    self._send_json(200, svc.delta(payload))
                    return
                if path == "/v1/search":
                    self._send_json(200, svc.search(payload, client_id=identity.client_id))
                    return
                if path == "/v1/read":
                    self._send_json(200, svc.read_note(payload, can_casual=identity.can_casual))
                    return
                if path == "/v1/write":
                    self._send_json(200, svc.write_note(payload, identity=identity))
                    return
                if path == "/v1/handoff":
                    self._send_json(200, svc.handoff(payload, identity=identity))
                    return
                if path == "/v1/diary":
                    self._send_json(200, svc.diary(payload, identity=identity))
                    return

            self._send_json(404, {"error": "not_found", "path": path})
        except AuthError as exc:
            self._send_json(exc.status_code, {"error": exc.error, "detail": exc.detail})
        except ServiceError as exc:
            self._send_json(exc.status_code, {"error": exc.error, "detail": exc.detail})
        except Exception as exc:
            self._send_json(500, {"error": "internal_error", "detail": str(exc)})

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")


def create_http_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    tokens_path: Path | None = None,
) -> _BrokerHTTPServer:
    """创建已绑定端口的 HTTP 服务实例（端口占用时抛出明确 OSError）。"""
    bind_port = port if port is not None else config.broker.port
    service = BrokerService(config, tokens_path=tokens_path)
    return _BrokerHTTPServer((host, bind_port), BrokerHTTPHandler, service)


def run_http_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    tokens_path: Path | None = None,
) -> int:
    """启动 HTTP 服务并支持 Ctrl+C 干净退出；端口占用时打印清晰错误。"""
    bind_port = port if port is not None else config.broker.port
    try:
        httpd = create_http_server(config, host=host, port=bind_port, tokens_path=tokens_path)
    except OSError as exc:
        print(f"ERROR: 无法绑定 {host}:{bind_port}（可能端口已被占用）: {exc}")
        return 1

    try:
        from agentbrain.agy_rules import sync_agy_rules

        _agy = sync_agy_rules(config)
        if _agy.get("changed"):
            print(f"agy 规则文件已刷新：{_agy['path']} ({_agy['chars']} 字)")
    except Exception as _exc:  # 绝不能因为写规则文件失败而拖死服务
        print(f"WARN: agy 规则同步失败（忽略）：{_exc}")

    print(f"AgentBrain HTTP server listening on http://{host}:{bind_port} (profile={config.memory.profile})")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nShutting down AgentBrain HTTP server...")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
