"""cli.py 单元测试：init / reindex / search / status / doctor。"""

from __future__ import annotations

import json
from pathlib import Path

from agentbrain.cli import main
from agentbrain.config import AppConfig


def test_cli_full_workflow_and_doctor_drift(
    isolated_env: AppConfig,
    capsys,
) -> None:
    note_path = isolated_env.profile_dir / "20_LongTerm" / "Facts" / "CLI测试.md"
    note_path.write_text(
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# CLI测试\n支付重构幂等设计\n- requires [[其他]]\n",
        encoding="utf-8",
    )

    assert main(["cli", "init"]) == 0
    assert main(["cli", "reindex"]) == 0
    out_reindex = capsys.readouterr().out
    assert "links=1" in out_reindex

    assert main(["cli", "status"]) == 0
    out_status = capsys.readouterr().out
    assert "index_inside_vault: False" in out_status

    assert main(["cli", "search", "幂等设计"]) == 0
    out_search = json.loads(capsys.readouterr().out)
    assert len(out_search["hits"]) == 1
    assert out_search["hits"][0]["title"] == "CLI测试"

    # 首次 doctor 记录基准，无漂移
    assert main(["cli", "doctor"]) == 0
    out_doc1 = capsys.readouterr().out
    assert "no drift" in out_doc1

    # 修改 SOUL.md 应检测到漂移
    Path(isolated_env.broker.soul_path).write_text("# SOUL\nChanged\n", encoding="utf-8")
    assert main(["cli", "doctor"]) == 1
    out_doc2 = capsys.readouterr().out
    assert "drift detected" in out_doc2
