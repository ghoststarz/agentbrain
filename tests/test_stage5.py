"""Stage 5 单元测试：Trigger 契约迁移、三段式召回、标题加权、服务端 Cooldown 与写入兜底。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agentbrain.acl import AuthIdentity
from agentbrain.config import AppConfig
from agentbrain.index import connect_db, init_db, reindex
from agentbrain.parse import parse_note
from agentbrain.retrieve import _make_excerpt, rank_and_budget, recall_fts, recall_trigger, search_notes
from agentbrain.server import BrokerService


def _write_note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_schema_migration_alter_table_in_place(isolated_env: AppConfig) -> None:
    db_path = isolated_env.broker.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                type TEXT,
                tags TEXT,
                status TEXT,
                category TEXT,
                confidence REAL,
                fact_key TEXT,
                valid_from TEXT,
                valid_to TEXT,
                episode TEXT,
                created TEXT,
                updated TEXT,
                last_activated TEXT,
                access_count INTEGER DEFAULT 0,
                pinned INTEGER DEFAULT 0,
                source TEXT,
                fm_json TEXT NOT NULL,
                mtime REAL NOT NULL,
                hash TEXT NOT NULL
            );
            INSERT INTO notes (path, title, fm_json, mtime, hash)
            VALUES ('legacy.md', 'legacy', '{}', 1.0, 'abc');
            """
        )

    init_db(isolated_env)
    with connect_db(db_path) as conn:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
        assert {"keys_json", "keys2_json", "trigger_order"}.issubset(cols)
        legacy = conn.execute("SELECT title, keys_json, keys2_json, trigger_order FROM notes WHERE path = 'legacy.md'").fetchone()
        assert legacy is not None
        assert legacy["title"] == "legacy"
        assert legacy["keys_json"] == "[]"
        assert legacy["keys2_json"] == "[]"
        assert int(legacy["trigger_order"]) == 100


def test_parse_note_keys_keys2_order_defaults_and_normalization() -> None:
    raw = """---
type: decision
keys: ["ADR-03", "同步器中枢", "ADR-03"]
keys2: ["唯一入口"]
order: 42
---
# ADR-03
正文
"""
    parsed = parse_note(Path("ADR-03.md"), raw)
    assert parsed.keys == ["ADR-03", "同步器中枢"]
    assert parsed.keys2 == ["唯一入口"]
    assert parsed.order == 42

    parsed_default = parse_note(Path("Plain.md"), "---\ntype: fact\n---\n# Plain\n")
    assert parsed_default.keys == []
    assert parsed_default.keys2 == []
    assert parsed_default.order == 100


def test_trigger_ascii_word_boundary_and_case_insensitive(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "ADR-03.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"ADR-03\"]\n---\n# ADR-03\n同步器必须是中枢。\n",
    )
    reindex(isolated_env)

    with connect_db(isolated_env.broker.db_path) as conn:
        assert len(recall_trigger(conn, "adr-03 的结论是什么")) == 1
        assert len(recall_trigger(conn, "这条ADR-03很重要")) == 1
        assert len(recall_trigger(conn, "ADR-030 是什么")) == 0


def test_trigger_keys2_disambiguation(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "中枢消歧测试.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"中枢\"]\nkeys2: [\"同步器\"]\n---\n# 中枢消歧测试\n同步器中枢。\n",
    )
    reindex(isolated_env)

    with connect_db(isolated_env.broker.db_path) as conn:
        assert recall_trigger(conn, "中枢") == []
        hits = recall_trigger(conn, "为什么同步器是中枢")
        assert len(hits) == 1
        assert hits[0].title == "中枢消歧测试"


def test_trigger_order_no_weak_evidence_and_fts_padding(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "后置触发.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"触发词\"]\norder: 200\n---\n# 后置触发\n内容一\n",
    )
    _write_note(
        d / "前置触发.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"触发词\"]\norder: 10\n---\n# 前置触发\n内容二\n",
    )
    _write_note(
        d / "FTS补充.md",
        "---\ntype: fact\ncategory: stable_profile\n---\n# FTS补充\n这里讨论触发词的补充细节与架构。\n",
    )
    reindex(isolated_env)

    with connect_db(isolated_env.broker.db_path) as conn:
        t_hits = recall_trigger(conn, "触发词")
        assert [h.title for h in t_hits] == ["前置触发", "后置触发"]
        assert all(not h.weak_evidence for h in t_hits)

        f_hits, bypassed = recall_fts(conn, "触发词 补充细节", ret_cfg=isolated_env.memory.retrieval)
        res = rank_and_budget(
            conn,
            t_hits,
            f_hits,
            k=3,
            hops=1,
            budget_limit=2400,
            evidence_bypassed=bypassed,
        )
        titles = [h["title"] for h in res["hits"]]
        assert titles[:2] == ["前置触发", "后置触发"]
        assert "FTS补充" in titles
        assert len(titles) == 3
        assert all("weak_evidence" not in h for h in res["hits"][:2])


def test_fts_title_and_identifier_weighting_independent_of_trigger(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm"
    _write_note(
        d / "Decisions" / "ADR-03-同步器必须是中枢.md",
        "---\ntype: decision\ncategory: stable_profile\n---\n# ADR-03 同步器必须是中枢\n共享记忆库同步器结论。\n",
    )
    _write_note(
        d / "Projects" / "共享记忆库总览.md",
        "---\ntype: project\ncategory: stable_profile\n---\n# 共享记忆库总览\n共享记忆库里有一条结论，关于 ADR-03 的讨论很多，共享记忆库结论在这里。\n",
    )
    _write_note(
        d / "Facts" / "其他笔记.md",
        "---\ntype: fact\ncategory: stable_profile\n---\n# 其他笔记\n共享记忆库日常记录。\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "我们的共享记忆库里有一条 ADR-03，它的结论是什么")
    assert res["hits"]
    assert res["hits"][0]["title"] == "ADR-03-同步器必须是中枢"


def test_server_cooldown_5_turns_and_empty_session_bypass(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Decisions"
    _write_note(
        d / "ADR-03.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"ADR-03\"]\n---\n# ADR-03\n同步器必须是中枢。\n",
    )
    _write_note(
        d / "备选笔记.md",
        "---\ntype: fact\ncategory: stable_profile\n---\n# 备选笔记\n关于 ADR-03 的背景说明。\n",
    )
    reindex(isolated_env)

    svc = BrokerService(isolated_env)
    rev_before = svc.healthz()["revision"]
    query = "ADR-03 的结论是什么"

    # 同一 (client, session) 连发 6 次：第 1 次含 ADR-03，第 2-5 次不含，第 6 次恢复含
    has_adr03: list[bool] = []
    for _ in range(6):
        res = svc.search({"query": query, "session_id": "sess-cd-1"}, client_id="lily")
        titles = [h["title"] for h in res["hits"]]
        has_adr03.append("ADR-03" in titles)
    assert has_adr03 == [True, False, False, False, False, True]

    # 不传 session_id 连发 3 次：每次都含
    for _ in range(3):
        res_stateless = svc.search({"query": query}, client_id="lily")
        assert "ADR-03" in [h["title"] for h in res_stateless["hits"]]

    # 不许递增 revision
    assert svc.healthz()["revision"] == rev_before


def test_write_note_explicit_keys_and_deterministic_fallback(isolated_env: AppConfig) -> None:
    svc = BrokerService(isolated_env)
    ident = AuthIdentity(client_id="lily", domain="agent", can_casual=False)

    # 1. 显式传入 keys / keys2 / order
    res1 = svc.write_note(
        {
            "title": "显式触发笔记",
            "content": "测试显式触发键。",
            "type": "decision",
            "keys": ["自定义触发"],
            "keys2": ["消歧词"],
            "order": 25,
        },
        identity=ident,
    )
    assert res1["ok"] is True
    note1 = parse_note(
        isolated_env.profile_dir / res1["path"],
        (isolated_env.profile_dir / res1["path"]).read_text(encoding="utf-8"),
    )
    assert note1.keys == ["自定义触发"]
    assert note1.keys2 == ["消歧词"]
    assert note1.order == 25

    # 2. 未传 keys 时的确定性兜底：只提取 fact_key 对象名、标题编号型标识符、显式 aliases
    #    绝不抓 tags、绝不抓出链标题
    res2 = svc.write_note(
        {
            "title": "ADR-09 v2 协议约定",
            "content": "协议正文。",
            "type": "decision",
            "fact_key": "协议.版本",
            "aliases": ["九号协议"],
            "tags": ["forbidden-tag", "do-not-extract"],
            "links": ["出链目标B", "ADR-03"],
        },
        identity=ident,
    )
    note2 = parse_note(
        isolated_env.profile_dir / res2["path"],
        (isolated_env.profile_dir / res2["path"]).read_text(encoding="utf-8"),
    )
    assert note2.keys == ["协议", "ADR-09", "v2", "九号协议"]
    assert "forbidden-tag" not in note2.keys
    assert "出链目标B" not in note2.keys
    assert "ADR-03" not in note2.keys


def test_make_excerpt_sentence_boundary_and_skip_headings() -> None:
    # 1. 跳过 frontmatter 残渣、前导文件名标题与 # 标题行，并在句末标点处截断
    raw = (
        "---\n"
        "type: decision\n"
        "---\n"
        "ADR-03-同步器必须是中枢\n"
        "# ADR-03 同步器必须是中枢\n\n"
        "## 决策结论\n"
        "第一句说明同步器是唯一权威中枢，统一承担解析、增量索引、证据检索与原子写入职责，确保多端一致性与可追溯性，并且持续保持高可用与确定性行为。"
        "第二句补充说明旧路径已作废，不得在 Vault 中放置运行态数据库，所有写入必须经过原子锁保护，并且支持幂等去重与增量重建以及多端共享同步。"
        "第三句如果继续拼接就会明显超过两百四十字的上限，因此应当在第二句句号处优雅截断，绝对不能停在半句上，这里再追加一段足够长的补充文本确保总长度明显超过两百四十字限制并触发边界截断逻辑，同时继续补充更多上下文细节以保证总字符数远超上限。\n"
    )
    excerpt = _make_excerpt(raw, title="ADR-03-同步器必须是中枢")
    assert not excerpt.startswith("#")
    assert not excerpt.startswith("---")
    assert not excerpt.startswith("ADR-03-同步器必须是中枢")
    assert excerpt.endswith("。")
    assert 100 <= len(excerpt) <= 240
    assert "第三句" not in excerpt

    # 2. 只有分号时回退到分号并加省略号
    semi_only = "前段描述" + ("字" * 165) + "；后段继续展开" + ("字" * 80)
    semi_excerpt = _make_excerpt(semi_only)
    assert semi_excerpt.endswith("…")
    assert len(semi_excerpt) <= 240
    assert "后段继续展开" not in semi_excerpt

    # 3. 整篇第一段无标点超长时才硬切并加省略号
    no_punct = "无标点长段落" * 50
    hard_excerpt = _make_excerpt(no_punct)
    assert hard_excerpt.endswith("…")
    assert len(hard_excerpt) == 240


def test_trigger_score_range_and_priority_over_higher_fts_score(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm"
    _write_note(
        d / "Decisions" / "ADR-03-同步器.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"ADR-03\"]\norder: 150\n---\n# ADR-03\n同步器是唯一中枢。\n",
    )
    _write_note(
        d / "Projects" / "高分FTS项目.md",
        "---\ntype: project\ncategory: stable_profile\n---\n# 高分FTS项目 ADR-03\nADR-03 同步器 结论 讨论 详情。\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "ADR-03 同步器 结论 讨论 详情", k_override=2)
    hits = res["hits"]
    assert len(hits) == 2
    assert hits[0]["title"] == "ADR-03-同步器"
    assert hits[0]["source"] == "trigger"
    assert 1.2 <= hits[0]["score"] <= 1.8
    assert all(0.0 <= h["score"] <= 3.0 for h in hits)


def test_search_hits_include_source_field_trigger_and_fts(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm"
    _write_note(
        d / "Decisions" / "触发笔记.md",
        "---\ntype: decision\ncategory: stable_profile\nkeys: [\"同步器\"]\norder: 50\n---\n# 触发笔记\n同步器中枢。\n",
    )
    _write_note(
        d / "Facts" / "检索笔记.md",
        "---\ntype: fact\ncategory: stable_profile\n---\n# 检索笔记\n同步器与幂等检索细节。\n",
    )
    reindex(isolated_env)

    res = search_notes(isolated_env, "同步器 幂等检索细节", k_override=2)
    sources = [h["source"] for h in res["hits"]]
    assert sources == ["trigger", "fts"]


def test_search_hits_include_source_fts_weak(isolated_env: AppConfig) -> None:
    d = isolated_env.profile_dir / "20_LongTerm" / "Facts"
    # 造多篇高 df 笔记，使得公共二字词 IDF 很低从而触发弱证据回退
    for idx in range(6):
        _write_note(
            d / f"通用记录{idx}.md",
            f"---\ntype: fact\ncategory: stable_profile\n---\n# 通用记录{idx}\n系统日常运行记录。\n",
        )
    reindex(isolated_env)

    res = search_notes(isolated_env, "系统", k_override=2)
    assert res.get("evidence_gate") == "bypassed"
    assert len(res["hits"]) >= 1
    assert all(h["source"] == "fts_weak" for h in res["hits"])
