"""测试公共夹具：构造隔离的临时 Vault 与运行时目录。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentbrain.config import AppConfig, load_config


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """创建隔离的临时 vault、SOUL.md、broker.yaml 与 memory.yaml。"""
    vault_root = tmp_path / "Asteria Lily" / "Memory"
    profile_dir = vault_root / "profiles" / "lily"
    (profile_dir / "20_LongTerm" / "Decisions").mkdir(parents=True, exist_ok=True)
    (profile_dir / "20_LongTerm" / "Facts").mkdir(parents=True, exist_ok=True)

    memory_yaml = {
        "schema": 1,
        "profile": "lily",
        "identity": {"mode": "pointer"},
        "retrieval": {
            "seed_k": 5,
            "hops": 1,
            "max_chars": 1200,
            "hard_max_chars": 2400,
            "half_life_days": 30,
            "superseded_penalty": 0.3,
            "min_evidence": 0.30,
        },
        "working": {"hot_max_chars": 1500},
        "diary": {"gate": False},
    }
    (profile_dir / "memory.yaml").write_text(
        yaml.safe_dump(memory_yaml, allow_unicode=True),
        encoding="utf-8",
    )

    runtime_dir = tmp_path / "runtime"
    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("# SOUL\nLily\n", encoding="utf-8")

    broker_yaml = {
        "vault_root": str(vault_root),
        "soul_path": str(soul_path),
        "port": 18765,
        "role": "primary",
        "index_dir": str(runtime_dir / "index"),
        "rev_dir": str(runtime_dir / "rev"),
        "state_dir": str(runtime_dir / "state"),
        "audit_dir": str(runtime_dir / "audit"),
        "log_dir": str(runtime_dir / "logs"),
    }
    broker_cfg_path = runtime_dir / "broker.yaml"
    broker_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    broker_cfg_path.write_text(
        yaml.safe_dump(broker_yaml, allow_unicode=True),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENTBRAIN_BROKER_CONFIG", str(broker_cfg_path))
    return load_config(broker_path=broker_cfg_path, profile="lily")
