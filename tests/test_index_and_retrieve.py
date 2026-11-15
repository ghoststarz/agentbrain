"""index.py 与 retrieve.py 单元测试：增量索引、事实键收敛、打分与证据门槛。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from agentbrain.config import AppConfig
from agentbrain.index import connect_db, reindex
from agentbrain.retrieve import search_notes


def _write_note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_incremental_reindex_add_skip_update_delete(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    n1 = d / "笔记一.md"
    _write_note(
        n1,
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 笔记一\n支付重构时发现幂等设计有漏洞\n",
    )

    s1 = reindex(isolated_env)
    assert s1.added == 1
    assert s1.total_notes == 1

    # 未改动再跑一次应跳过
    s2 = reindex(isolated_env)
    assert s2.skipped == 1
    assert s2.added == 0
    assert s2.updated == 0

    # 修改内容应更新
    _write_note(
        n1,
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 笔记一\n支付重构时发现幂等设计有漏洞（已补充）\n",
    )
    s3 = reindex(isolated_env)
    assert s3.updated == 1

    # 删除文件应清出索引
    n1.unlink()
    s4 = reindex(isolated_env)
    assert s4.deleted == 1
    assert s4.total_notes == 0


def test_fact_key_convergence_and_superseded_penalty(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    old_note = d / "旧决策.md"
    new_note = d / "新决策.md"

    _write_note(
        old_note,
        "---\ntype: decision\ncreated: 2026-11-01\nfact_key: 记忆库.位置\ncategory: stable_profile\n---\n# 旧决策\n幂等设计旧方案\n",
    )
    _write_note(
        new_note,
        "---\ntype: decision\ncreated: 2026-11-14\nfact_key: 记忆库.位置\ncategory: stable_profile\n---\n# 新决策\n幂等设计新方案\n- requires [[旧决策]]\n",
    )

    stats = reindex(isolated_env)
    assert stats.converged_facts == 1
    assert old_note.exists()  # 不删文件

    with connect_db(isolated_env.broker.db_path) as conn:
        row = conn.execute("SELECT fm_json FROM notes WHERE title = '旧决策'").fetchone()
        fm = json.loads(row["fm_json"])
        assert fm["superseded_by"] == "[[新决策]]"

    res = search_notes(isolated_env, "幂等设计")
    titles = [h["title"] for h in res["hits"]]
    # 两篇都返回，且新决策排在前面（旧决策被 superseded_penalty 降权）
    assert "新决策" in titles
    assert "旧决策" in titles
    assert titles.index("新决策") < titles.index("旧决策")
    # 检查 connected[].context_line
    new_hit = next(h for h in res["hits"] if h["title"] == "新决策")
    assert any(
        c["relation"] == "requires" and c["context_line"] == "- requires [[旧决策]]"
        for c in new_hit["connected"]
    )


def test_evidence_threshold_blocks_single_char_and_missing(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    _write_note(
        d / "幂等说明.md",
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 幂等说明\n接口必须保证幂等性。\n",
    )
    reindex(isolated_env)

    # 单字不计入证据 -> 0 命中
    res_single = search_notes(isolated_env, "幂")
    assert res_single["hits"] == []

    # 无关词 -> 0 命中
    res_none = search_notes(isolated_env, "退款")
    assert res_none["hits"] == []


def test_decay_and_validity_window(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    _write_note(
        d / "稳定画像.md",
        "---\ntype: fact\ncreated: 2020-01-01\ncategory: stable_profile\n---\n# 稳定画像\n分布式锁原理\n",
    )
    _write_note(
        d / "过期事件.md",
        "---\ntype: fact\ncreated: 2020-01-01\nvalid_to: 2021-01-01\ncategory: event\n---\n# 过期事件\n分布式锁原理\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "分布式锁", today=dt.date(2026, 11, 14))
    assert len(res["hits"]) == 2
    assert res["hits"][0]["title"] == "稳定画像"
    assert res["hits"][0]["score"] > res["hits"][1]["score"]


def test_budget_truncation(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    for i in range(6):
        _write_note(
            d / f"背景文档{i}.md",
            f"---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 背景文档{i}\n其他无关记录 {i}\n",
        )
    for i in range(4):
        _write_note(
            d / f"测试文档{i}.md",
            f"---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 测试文档{i}\n幂等设计实践详情 {'说明内容' * 30}\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "幂等设计", max_chars_override=350)
    assert res["truncated"] is True
    assert len(res["hits"]) < 4


def test_weak_evidence_fallback_small_homogeneous_vault(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    _write_note(
        d / "身高记录一.md",
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 身高记录一\n去年体检身高 175cm。\n",
    )
    _write_note(
        d / "身高记录二.md",
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 身高记录二\n今年复查身高依然 175cm。\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "身高")
    assert len(res["hits"]) >= 1
    assert res.get("evidence_gate") == "bypassed"
    assert all(h.get("weak_evidence") is True for h in res["hits"])


def test_weak_evidence_fallback_high_df_keyword(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    for i in range(10):
        extra = "；其中同步器负责增量拉取。" if i == 0 else ""
        _write_note(
            d / f"记忆笔记{i}.md",
            f"---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 记忆笔记{i}\n这是一篇关于记忆系统的第 {i} 篇笔记{extra}\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "记忆")
    assert len(res["hits"]) >= 1
    assert res.get("evidence_gate") == "bypassed"
    assert all(h.get("weak_evidence") is True for h in res["hits"])


def test_single_char_noise_still_blocked(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    _write_note(
        d / "日常笔记.md",
        "---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 日常笔记\n这是我的测试笔记。\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "的")
    assert res["hits"] == []
    assert "evidence_gate" not in res


def test_strong_evidence_normal_path_has_no_weak_flag(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    for i in range(10):
        extra = " 同步器只更新变更文件。" if i == 0 else ""
        _write_note(
            d / f"系统笔记{i}.md",
            f"---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n# 系统笔记{i}\n这是关于记忆系统的第 {i} 篇笔记。{extra}\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "同步器")
    assert len(res["hits"]) == 1
    assert res["hits"][0]["title"] == "系统笔记0"
    assert "evidence_gate" not in res
    assert all("weak_evidence" not in h for h in res["hits"])


def test_converged_facts_zero_on_repeated_reindex(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "旧方案.md",
        "---\ntype: decision\ncreated: 2026-11-01\nfact_key: db.engine\ncategory: stable_profile\n---\n# 旧方案\n旧方案内容\n",
    )
    _write_note(
        d / "新方案.md",
        "---\ntype: decision\ncreated: 2026-11-14\nfact_key: db.engine\ncategory: stable_profile\n---\n# 新方案\n新方案内容\n",
    )

    s1 = reindex(isolated_env)
    assert s1.converged_facts == 1

    s2 = reindex(isolated_env)
    assert s2.converged_facts == 0


def test_natural_language_long_query_recall(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "ADR-03.md",
        "---\ntype: decision\ncreated: 2026-11-14\ncategory: stable_profile\n---\n"
        "# ADR-03 同步器中枢架构\n结论：同步器是共享记忆库的唯一入口与中枢。\n",
    )
    for i in range(9):
        _write_note(
            d / f"其他决策{i}.md",
            f"---\ntype: decision\ncreated: 2026-11-14\ncategory: stable_profile\n---\n"
            f"# 其他决策{i}\n第 {i} 条常规记录，与共享记忆库无关。\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "我们的共享记忆库里有一条 ADR-03，它的结论是什么")
    assert len(res["hits"]) >= 1
    assert "ADR-03" in res["hits"][0]["title"]
    assert "weak_evidence" not in res["hits"][0]


def test_long_query_stop_tokens_do_not_crowd_out_adr03(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "ADR-03.md",
        "---\ntype: decision\ncreated: 2026-11-14\ncategory: stable_profile\n---\n"
        "# ADR-03 同步器必须是中枢\n结论：同步器作为唯一权威中枢。\n",
    )
    # 干扰笔记含大量高 IDF 功能词（小语料下 df=1），若不停用词过滤会抢占权重
    _write_note(
        d / "ADR-02.md",
        "---\ntype: decision\ncreated: 2026-11-14\ncategory: stable_profile\n---\n"
        "# ADR-02 中文检索用二元组\n我们为什么需要它？它的作用是什么，如果有一条规则就是这样。\n",
    )
    for i in range(8):
        _write_note(
            d / f"其他笔记{i}.md",
            f"---\ntype: fact\ncreated: 2026-11-14\ncategory: stable_profile\n---\n"
            f"# 其他笔记{i}\n第 {i} 条常规记录。\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "我们的共享记忆库里有一条 ADR-03，它的结论是什么")
    top3_titles = [h["title"] for h in res["hits"][:3]]
    assert any("ADR-03" in t for t in top3_titles)
