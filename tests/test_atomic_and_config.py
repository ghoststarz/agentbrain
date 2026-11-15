"""atomic.py 与 config.py 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agentbrain.atomic import atomic_write_json, atomic_write_text
from agentbrain.config import AppConfig


def test_atomic_write_text_utf8_no_bom_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "中文 目录" / "note.md"
    atomic_write_text(target, "第一行\r\n第二行\r第三行\n")
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.decode("utf-8") == "第一行\n第二行\n第三行\n"
    # 确保无残留临时文件
    assert [p.name for p in target.parent.iterdir()] == ["note.md"]


def test_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "handoff.json"
    payload = {"tasks": {"t1": {"状态": "进行中"}}}
    atomic_write_json(target, payload)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == payload


def test_config_paths_with_spaces_and_chinese(isolated_env: AppConfig) -> None:
    assert "Asteria Lily" in str(isolated_env.broker.vault_root)
    assert isolated_env.memory.profile == "lily"
    assert isolated_env.memory.diary_gate is False
    assert isolated_env.memory.retrieval.min_evidence == 0.30
