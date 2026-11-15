"""配置加载与路径解析模块，读取 broker.yaml 与 memory.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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

    @property
    def db_path(self) -> Path:
        """返回 SQLite 索引库文件路径（位于运行时目录，不进入 vault）。"""
        return self.index_dir / "agentbrain.db"

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


def _resolve_path(raw: str | Path) -> Path:
    """解析路径并展开环境变量，保留空格与中文字符。"""
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    return Path(expanded).resolve()


def load_broker_config(path: str | Path | None = None) -> tuple[BrokerConfig, Path]:
    """加载本机 broker.yaml。"""
    cfg_path = Path(path) if path is not None else default_broker_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到 broker.yaml: {cfg_path}")

    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
    )
    return broker, cfg_path


def load_memory_config(vault_root: Path, profile: str = "lily") -> tuple[MemoryConfig, Path]:
    """加载 vault 内指定 profile 的 memory.yaml。"""
    mem_path = vault_root / "profiles" / profile / "memory.yaml"
    if not mem_path.exists():
        raise FileNotFoundError(f"找不到 memory.yaml: {mem_path}")

    raw: dict[str, Any] = yaml.safe_load(mem_path.read_text(encoding="utf-8")) or {}
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

    memory = MemoryConfig(
        schema=int(raw.get("schema", 1)),
        profile=str(raw.get("profile", profile)),
        identity_mode=str(identity_raw.get("mode", "pointer")),
        clients=dict(raw.get("clients") or {}),
        inject=dict(raw.get("inject") or {}),
        retrieval=retrieval,
        hot_max_chars=int(working_raw.get("hot_max_chars", 1500)),
        diary_gate=bool(diary_raw.get("gate", False)),
    )
    return memory, mem_path


def load_config(
    broker_path: str | Path | None = None,
    profile: str = "lily",
) -> AppConfig:
    """加载完整应用配置。"""
    broker, b_path = load_broker_config(broker_path)
    memory, m_path = load_memory_config(broker.vault_root, profile=profile)
    return AppConfig(
        broker=broker,
        memory=memory,
        broker_config_path=b_path,
        memory_config_path=m_path,
    )
