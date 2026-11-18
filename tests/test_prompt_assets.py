"""提示词资产加载器测试（外部 md 手改即时生效，缺失/为空必须明确报错）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agentbrain.prompt_lib import PromptAssetError, list_prompts, load_prompt, prompt_dir


def test_review_system_prompt_loads_with_core_rules() -> None:
    text = load_prompt("review_system")
    assert "no_change" in text
    assert "expected_hash" in text
    assert "日记" in text


def test_agy_tool_rules_prompt_available() -> None:
    text = load_prompt("agy_tool_rules")
    assert "memory_search" in text


def test_list_prompts_covers_shipped_assets() -> None:
    names = list_prompts()
    for expected in ("review_system", "review_retry_compact", "agy_tool_rules"):
        assert expected in names


def test_missing_prompt_raises_instead_of_silently_empty() -> None:
    with pytest.raises(PromptAssetError):
        load_prompt("does_not_exist")


def test_prompt_edit_takes_effect_without_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import agentbrain.prompt_lib as lib

    (tmp_path / "probe.md").write_text("first", encoding="utf-8")
    monkeypatch.setattr(lib, "prompt_dir", lambda: tmp_path)
    lib._CACHE.clear()
    assert load_prompt("probe") == "first"

    time.sleep(0.01)
    (tmp_path / "probe.md").write_text("second version", encoding="utf-8")
    assert load_prompt("probe") == "second version"
    lib._CACHE.clear()
