"""配置加载与路径解析模块，读取 broker.yaml 与 memory.yaml。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


def default_broker_config_path() -> Path:
    """获取默认的本机 broker.yaml 路径。"""
    env_override = os.environ.get("AGENTBRAIN_BROKER_CONFIG")
    if env_override:
        return Path(env_override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "agentbrain" / "broker.yaml"
    return Path.home() / "AppData" / "Local" / "agentbrain" / "broker.yaml"


@dataclass(frozen=True)
class BrokerConfig:
    """本机运行的 broker.yaml 配置。"""

    vault_root: Path
    soul_path: Path
    port: int
    role: str
    index_dir: Path
    rev_dir: Path
    state_dir: Path
    audit_dir: Path
    log_dir: Path
    # agy（Antigravity CLI）全局规则文件路径。为 None 时用 ~/.gemini/GEMINI.md。
    # 必须在配置里可覆盖：否则任何用临时 config 跑起来的实例
    # （测试、第二份 vault、只读副本）都会写到真实的用户目录里，
    # 把真人格的 GEMINI.md 覆盖成测试夹具。
    agy_rules_path: Path | None = None
    # 可选的机器本地覆盖配置路径；为 None 时默认取 broker.yaml 同目录下的 memory.local.yaml。
    memory_local_path: Path | None = None

    @property
    def db_path(self) -> Path:
        """返回 SQLite 索引库文件路径（位于运行时目录，不进入 vault）。"""
        return self.index_dir / "agentbrain.db"

    def proactive_db_path(self, profile: str = "lily") -> Path:
        """返回指定 profile 的主动评审调度 SQLite 数据库路径（位于 state_dir，不进入 vault）。"""
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in profile.strip()) or "default"
        return self.state_dir / "proactive" / f"{safe}.db"

    def ensure_runtime_dirs(self) -> None:
        """确保所有运行时目录存在。"""
        for d in (
            self.index_dir,
            self.rev_dir,
            self.state_dir,
            self.audit_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class RetrievalConfig:
    """检索与打分参数。"""

    seed_k: int = 5
    hops: int = 1
    max_chars: int = 1200
    hard_max_chars: int = 2400
    half_life_days: float = 30.0
    superseded_penalty: float = 0.3
    min_evidence: float = 0.30


class ProactiveConfigError(ValueError):
    """主动整理配置格式或安全校验失败（fail-closed）。"""


def _require_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    if key not in raw or raw[key] is None:
        return default
    val = raw[key]
    if not isinstance(val, bool):
        raise ProactiveConfigError(f"proactive.{key} 必须为布尔值，收到: {val!r}")
    return val


@dataclass(frozen=True)
class ProactiveConfig:
    """主动整理与后台评审调度配置（默认关闭）。"""

    enabled: bool = False
    event_capture: bool = False
    incremental_batch_size: int = 5
    nightly_hour: int = 23
    nightly_minute: int = 0
    lease_seconds: int = 300
    max_attempts: int = 5
    base_backoff_seconds: int = 30
    max_backoff_seconds: int = 3600
    max_payload_chars: int = 16000
    max_payload_chunks: int = 16
    retention_days: int = 30
    agy_ide_enabled: bool = False
    agy_ide_roots: tuple[Path, ...] = ()
    agy_ide_poll_seconds: int = 30
    worker_poll_seconds: int = 15
    model_endpoint: str = ""
    model_name: str = ""
    model_api_key_env: str = ""
    model_backend: str = ""
    hermes_profile: str = ""
    hermes_home: str = ""
    hermes_agent_dir: str = ""
    hermes_python: str = ""
    hermes_statedb_enabled: bool = True
    hermes_state_db_path: str = ""
    model_timeout_seconds: float = 30.0
    model_max_context_chars: int = 24000
    model_max_output_tokens: int = 8192
    archive_enabled: bool = False
    archive_after_days: int = 14
    nightly_always_diary: bool = False
    prefetch_min_score: float = 1.0
    section_audit_enabled: bool = False
    section_audit_min_similarity: float = 0.45
    section_audit_max_files: int = 3
    nightly_recent_diary_days: int = 3
    nightly_recent_diary_max_chars: int = 4000
    nightly_today_diary_max_chars: int = 3000
    nightly_split_enabled: bool = True
    nightly_split_threshold_chars: int = 0
    nightly_enabled: bool = True
    diary_dedupe_similarity: float = 0.60
    diary_max_entries_per_day: int = 0


_KNOWN_PROACTIVE_KEYS = frozenset(f.name for f in fields(ProactiveConfig))


_VALID_MODEL_BACKENDS = frozenset({"", "auto", "hermes", "openai_compatible"})


def resolve_effective_model_backend(cfg: ProactiveConfig) -> str:
    """解析生效的后台评审模型通道：默认使用 Hermes 框架路由，显式配置或遗留端点则走 OpenAI 兼容。"""
    raw = cfg.model_backend.strip().lower()
    if raw not in _VALID_MODEL_BACKENDS:
        raise ProactiveConfigError(
            f"proactive.model_backend 无效: {cfg.model_backend!r}（仅支持 hermes / openai_compatible）"
        )
    if raw in ("hermes", "openai_compatible"):
        return raw
    if cfg.model_endpoint.strip() or cfg.model_name.strip():
        return "openai_compatible"
    return "hermes"


def _validate_hermes_profile_name(profile: str) -> None:
    p = profile.strip()
    if not p:
        return
    if "/" in p or "\\" in p or ".." in p or "\x00" in p:
        raise ProactiveConfigError(f"proactive.hermes_profile 包含非法路径字符: {profile!r}")


def validate_proactive_worker_config(cfg: ProactiveConfig) -> None:
    """校验主动评审 worker 发起模型请求前的配置完整性（fail-closed）。"""
    if not cfg.enabled:
        raise ProactiveConfigError("proactive.enabled 为 false，禁止后台模型调用")
    if cfg.model_timeout_seconds <= 0:
        raise ProactiveConfigError("proactive.model_timeout_seconds 必须 > 0")
    if cfg.model_max_context_chars < 512:
        raise ProactiveConfigError("proactive.model_max_context_chars 必须 >= 512")
    if cfg.model_max_output_tokens < 64:
        raise ProactiveConfigError("proactive.model_max_output_tokens 必须 >= 64")

    backend = resolve_effective_model_backend(cfg)
    if backend == "openai_compatible":
        ep = cfg.model_endpoint.strip()
        if not (ep.startswith("http://") or ep.startswith("https://")):
            raise ProactiveConfigError(f"proactive.model_endpoint 必须为 http(s) URL: {ep!r}")
        if not cfg.model_name.strip():
            raise ProactiveConfigError("proactive.model_name 不能为空")
        env_name = cfg.model_api_key_env.strip()
        if env_name:
            if (
                env_name.startswith(("sk-", "ghp_", "AIza", "xox"))
                or "=" in env_name
                or any(ch.isspace() for ch in env_name)
                or not env_name.replace("_", "").isalnum()
            ):
                raise ProactiveConfigError(
                    "proactive.model_api_key_env 必须是环境变量名，不能直接填写密钥"
                )
        return

    _validate_hermes_profile_name(cfg.hermes_profile)
    for field_name, val in (
        ("hermes_home", cfg.hermes_home),
        ("hermes_agent_dir", cfg.hermes_agent_dir),
        ("hermes_python", cfg.hermes_python),
    ):
        if "\x00" in val:
            raise ProactiveConfigError(f"proactive.{field_name} 包含非法空字节")


@dataclass(frozen=True)
class MemoryConfig:
    """记忆域 memory.yaml 配置。"""

    schema: int = 1
    profile: str = "lily"
    identity_mode: str = "pointer"
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    inject: dict[str, dict[str, Any]] = field(default_factory=dict)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    hot_max_chars: int = 1500
    diary_gate: bool = False
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)


@dataclass(frozen=True)
class AppConfig:
    """聚合后的完整配置对象。"""

    broker: BrokerConfig
    memory: MemoryConfig
    broker_config_path: Path
    memory_config_path: Path

    @property
    def profile_dir(self) -> Path:
        """返回当前记忆域在 vault 中的根目录。"""
        return self.broker.vault_root / "profiles" / self.memory.profile

    @property
    def proactive_db_path(self) -> Path:
        """返回当前 profile 的主动评审调度 SQLite 数据库路径（位于 state_dir）。"""
        return self.broker.proactive_db_path(self.memory.profile)


_BRACE_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_portable_path(raw: str | Path) -> str:
    """展开 ~、%VAR%、$VAR 与 ${VAR} 环境变量，返回规范化字符串。"""
    text = str(raw).strip()
    if not text:
        return ""
    text = _BRACE_ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), text)
    return os.path.expandvars(os.path.expanduser(text))


def _resolve_path(raw: str | Path) -> Path:
    """解析路径并展开环境变量，保留空格与中文字符。"""
    return Path(expand_portable_path(raw)).resolve()


def load_broker_config(path: str | Path | None = None) -> tuple[BrokerConfig, Path]:
    """加载本机 broker.yaml。"""
    cfg_path = Path(path) if path is not None else default_broker_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到 broker.yaml: {cfg_path}")

    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    agy_raw = raw.get("agy_rules_path")
    local_raw = raw.get("memory_local_path")
    broker = BrokerConfig(
        vault_root=_resolve_path(raw["vault_root"]),
        soul_path=_resolve_path(raw["soul_path"]),
        port=int(raw.get("port", 18765)),
        role=str(raw.get("role", "primary")),
        index_dir=_resolve_path(raw["index_dir"]),
        rev_dir=_resolve_path(raw["rev_dir"]),
        state_dir=_resolve_path(raw["state_dir"]),
        audit_dir=_resolve_path(raw["audit_dir"]),
        log_dir=_resolve_path(raw["log_dir"]),
        agy_rules_path=_resolve_path(agy_raw) if agy_raw else None,
        memory_local_path=_resolve_path(local_raw) if local_raw else None,
    )
    return broker, cfg_path


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个映射：dict 逐层合并，list 与标量整体替换。"""
    merged: dict[str, Any] = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge_dicts(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_memory_config(
    vault_root: Path,
    profile: str = "lily",
    *,
    local_override_path: Path | None = None,
) -> tuple[MemoryConfig, Path]:
    """加载 vault 内指定 profile 的 memory.yaml，并可选深度合并本机覆盖层。"""
    mem_path = vault_root / "profiles" / profile / "memory.yaml"
    if not mem_path.exists():
        raise FileNotFoundError(f"找不到 memory.yaml: {mem_path}")

    raw_loaded = yaml.safe_load(mem_path.read_text(encoding="utf-8"))
    if raw_loaded is None:
        raw: dict[str, Any] = {}
    elif isinstance(raw_loaded, dict):
        raw = raw_loaded
    else:
        raise ProactiveConfigError("memory.yaml 顶层必须为映射对象")

    if local_override_path is not None and local_override_path.exists():
        override_loaded = yaml.safe_load(local_override_path.read_text(encoding="utf-8"))
        if override_loaded is not None:
            if not isinstance(override_loaded, dict):
                raise ProactiveConfigError(
                    f"memory.local.yaml 顶层必须为映射对象: {local_override_path}"
                )
            raw = _deep_merge_dicts(raw, override_loaded)

    ret_raw = raw.get("retrieval") or {}
    retrieval = RetrievalConfig(
        seed_k=int(ret_raw.get("seed_k", 5)),
        hops=int(ret_raw.get("hops", 1)),
        max_chars=int(ret_raw.get("max_chars", 1200)),
        hard_max_chars=int(ret_raw.get("hard_max_chars", 2400)),
        half_life_days=float(ret_raw.get("half_life_days", 30)),
        superseded_penalty=float(ret_raw.get("superseded_penalty", 0.3)),
        min_evidence=float(ret_raw.get("min_evidence", 0.30)),
    )
    identity_raw = raw.get("identity") or {}
    working_raw = raw.get("working") or {}
    diary_raw = raw.get("diary") or {}
    proactive_raw = raw.get("proactive")
    if proactive_raw is None:
        proactive_raw = {}
    elif not isinstance(proactive_raw, dict):
        raise ProactiveConfigError("memory.yaml 中的 proactive 必须为映射对象")

    unknown_proactive = sorted(set(proactive_raw.keys()) - _KNOWN_PROACTIVE_KEYS)
    if unknown_proactive:
        raise ProactiveConfigError(
            f"proactive 包含未知配置键: {', '.join(unknown_proactive)}"
        )

    enabled = _require_bool(proactive_raw, "enabled", False)
    event_capture = _require_bool(proactive_raw, "event_capture", False)
    agy_ide_enabled = _require_bool(proactive_raw, "agy_ide_enabled", False)
    nightly_enabled = _require_bool(proactive_raw, "nightly_enabled", True)

    nightly_hour = int(proactive_raw.get("nightly_hour", 23))
    nightly_minute = int(proactive_raw.get("nightly_minute", 0))
    if not (0 <= nightly_hour <= 23) or not (0 <= nightly_minute <= 59):
        raise ProactiveConfigError(
            f"proactive.nightly_hour/minute 超出范围: {nightly_hour}:{nightly_minute}"
        )

    raw_roots = proactive_raw.get("agy_ide_roots")
    if raw_roots is None:
        agy_ide_roots: tuple[Path, ...] = ()
    elif isinstance(raw_roots, list):
        agy_ide_roots = tuple(_resolve_path(p) for p in raw_roots if str(p).strip())
    else:
        raise ProactiveConfigError("proactive.agy_ide_roots 必须为路径列表")

    model_api_key_env = str(proactive_raw.get("model_api_key_env") or "").strip()
    if model_api_key_env and (
        model_api_key_env.startswith(("sk-", "ghp_", "AIza", "xox"))
        or "=" in model_api_key_env
        or any(ch.isspace() for ch in model_api_key_env)
    ):
        raise ProactiveConfigError(
            "proactive.model_api_key_env 必须是环境变量名，不能直接存放密钥"
        )

    model_timeout_seconds = float(proactive_raw.get("model_timeout_seconds", 30.0))
    model_max_context_chars = int(proactive_raw.get("model_max_context_chars", 24000))
    model_max_output_tokens = int(proactive_raw.get("model_max_output_tokens", 8192))
    if enabled and (
        model_timeout_seconds <= 0
        or model_max_context_chars < 512
        or model_max_output_tokens < 64
    ):
        raise ProactiveConfigError("proactive 已启用但模型超时或上下文预算参数非法")

    section_audit_enabled = _require_bool(proactive_raw, "section_audit_enabled", False)
    section_audit_min_similarity = float(proactive_raw.get("section_audit_min_similarity", 0.45))
    if not (0.0 <= section_audit_min_similarity <= 1.0):
        raise ProactiveConfigError(
            f"proactive.section_audit_min_similarity 必须在 [0.0, 1.0] 范围内，收到: {section_audit_min_similarity!r}"
        )
    section_audit_max_files = max(1, int(proactive_raw.get("section_audit_max_files", 3)))
    diary_dedupe_similarity = float(proactive_raw.get("diary_dedupe_similarity", 0.60))
    if not (0.0 <= diary_dedupe_similarity <= 1.0):
        raise ProactiveConfigError(
            f"proactive.diary_dedupe_similarity 必须在 [0.0, 1.0] 范围内，收到: {diary_dedupe_similarity!r}"
        )
    raw_diary_max = proactive_raw.get("diary_max_entries_per_day")
    diary_max_entries_per_day = max(0, int(0 if raw_diary_max is None else raw_diary_max))

    proactive = ProactiveConfig(
        enabled=enabled,
        event_capture=event_capture,
        incremental_batch_size=max(1, int(proactive_raw.get("incremental_batch_size", 5))),
        nightly_hour=nightly_hour,
        nightly_minute=nightly_minute,
        lease_seconds=max(1, int(proactive_raw.get("lease_seconds", 300))),
        max_attempts=max(1, int(proactive_raw.get("max_attempts", 5))),
        base_backoff_seconds=max(1, int(proactive_raw.get("base_backoff_seconds", 30))),
        max_backoff_seconds=max(1, int(proactive_raw.get("max_backoff_seconds", 3600))),
        max_payload_chars=max(256, int(proactive_raw.get("max_payload_chars", 16000))),
        max_payload_chunks=max(1, int(proactive_raw.get("max_payload_chunks", 16))),
        retention_days=max(1, int(proactive_raw.get("retention_days", 30))),
        agy_ide_enabled=agy_ide_enabled,
        agy_ide_roots=agy_ide_roots,
        agy_ide_poll_seconds=max(5, int(proactive_raw.get("agy_ide_poll_seconds", 30))),
        worker_poll_seconds=max(1, int(proactive_raw.get("worker_poll_seconds", 15))),
        model_endpoint=str(proactive_raw.get("model_endpoint") or "").strip(),
        model_name=str(proactive_raw.get("model_name") or "").strip(),
        model_api_key_env=model_api_key_env,
        model_backend=str(proactive_raw.get("model_backend") or "").strip(),
        hermes_profile=str(proactive_raw.get("hermes_profile") or "").strip(),
        hermes_home=expand_portable_path(str(proactive_raw.get("hermes_home") or "")),
        hermes_agent_dir=expand_portable_path(str(proactive_raw.get("hermes_agent_dir") or "")),
        hermes_python=expand_portable_path(str(proactive_raw.get("hermes_python") or "")),
        hermes_statedb_enabled=_require_bool(proactive_raw, "hermes_statedb_enabled", True),
        hermes_state_db_path=expand_portable_path(str(proactive_raw.get("hermes_state_db_path") or "")),
        model_timeout_seconds=model_timeout_seconds,
        model_max_context_chars=model_max_context_chars,
        model_max_output_tokens=model_max_output_tokens,
        archive_enabled=_require_bool(proactive_raw, "archive_enabled", False),
        archive_after_days=max(1, int(proactive_raw.get("archive_after_days", 14))),
        nightly_always_diary=_require_bool(proactive_raw, "nightly_always_diary", False),
        prefetch_min_score=float(proactive_raw.get("prefetch_min_score", 1.0)),
        section_audit_enabled=section_audit_enabled,
        section_audit_min_similarity=section_audit_min_similarity,
        section_audit_max_files=section_audit_max_files,
        nightly_recent_diary_days=max(0, int(proactive_raw.get("nightly_recent_diary_days", 3))),
        nightly_recent_diary_max_chars=max(0, int(proactive_raw.get("nightly_recent_diary_max_chars", 4000))),
        nightly_today_diary_max_chars=max(0, int(proactive_raw.get("nightly_today_diary_max_chars", 3000))),
        nightly_split_enabled=_require_bool(proactive_raw, "nightly_split_enabled", True),
        nightly_split_threshold_chars=max(0, int(proactive_raw.get("nightly_split_threshold_chars", 0))),
        nightly_enabled=nightly_enabled,
        diary_dedupe_similarity=diary_dedupe_similarity,
        diary_max_entries_per_day=diary_max_entries_per_day,
    )
    if enabled:
        validate_proactive_worker_config(proactive)

    memory = MemoryConfig(
        schema=int(raw.get("schema", 1)),
        profile=str(raw.get("profile", profile)),
        identity_mode=str(identity_raw.get("mode", "pointer")),
        clients=dict(raw.get("clients") or {}),
        inject=dict(raw.get("inject") or {}),
        retrieval=retrieval,
        hot_max_chars=int(working_raw.get("hot_max_chars", 1500)),
        diary_gate=bool(diary_raw.get("gate", False)),
        proactive=proactive,
    )
    return memory, mem_path


def load_config(
    broker_path: str | Path | None = None,
    profile: str = "lily",
) -> AppConfig:
    """加载完整应用配置。"""
    broker, b_path = load_broker_config(broker_path)
    local_override = (
        broker.memory_local_path
        if broker.memory_local_path is not None
        else (b_path.parent / "memory.local.yaml")
    )
    memory, m_path = load_memory_config(
        broker.vault_root,
        profile=profile,
        local_override_path=local_override,
    )
    return AppConfig(
        broker=broker,
        memory=memory,
        broker_config_path=b_path,
        memory_config_path=m_path,
    )
