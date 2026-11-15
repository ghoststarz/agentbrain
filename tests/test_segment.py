"""segment.py 单元测试：CJK 重叠二元组、ASCII 小写、单字不计入证据。"""

from __future__ import annotations

from agentbrain.segment import CJK_STOP_TOKENS, is_evidence_token, segment_query, segment_text


def test_cjk_overlapping_bigrams_example() -> None:
    text = "支付重构时发现幂等设计有漏洞"
    tokens = segment_text(text)
    assert tokens == [
        "支付", "付重", "重构", "构时", "时发", "发现",
        "现幂", "幂等", "等设", "设计", "计有", "有漏", "漏洞",
    ]


def test_query_cjk_phrase() -> None:
    expr, all_toks, ev_toks = segment_query("幂等设计")
    assert expr == '"幂等" OR "等设" OR "设计"'
    assert all_toks == ["幂等", "等设", "设计"]
    assert ev_toks == ["幂等", "等设", "设计"]


def test_query_ascii_lowercase() -> None:
    expr, all_toks, ev_toks = segment_query("Redis")
    assert expr == '"redis"'
    assert all_toks == ["redis"]
    assert ev_toks == ["redis"]


def test_single_cjk_kept_as_token_but_not_evidence() -> None:
    tokens = segment_text("在Redis里")
    assert tokens == ["在", "redis", "里"]
    assert not is_evidence_token("在")
    assert not is_evidence_token("里")
    assert is_evidence_token("redis")

    expr, all_toks, ev_toks = segment_query("幂")
    assert expr == '"幂"'
    assert all_toks == ["幂"]
    assert ev_toks == []


def test_query_or_excludes_single_cjk_and_and_for_single_cjk_only() -> None:
    expr, all_toks, ev_toks = segment_query("在Redis里")
    assert expr == '"redis"'
    assert all_toks == ["在", "redis", "里"]
    assert ev_toks == ["redis"]

    expr2, all_toks2, ev_toks2 = segment_query("在 的")
    assert expr2 == '"在" AND "的"'
    assert all_toks2 == ["在", "的"]
    assert ev_toks2 == []


def test_stop_tokens_filtered_from_query_only() -> None:
    query = "我们的共享记忆库里有一条 ADR-03，它的结论是什么"
    # 索引侧 segment_text 不受停用词影响
    raw = segment_text(query)
    assert "我们" in raw
    assert "什么" in raw

    expr, all_toks, ev_toks = segment_query(query)
    assert "我们" in all_toks
    assert "什么" in all_toks
    for stop_tok in ("我们", "们的", "的共", "库里", "里有", "有一", "一条", "它的", "的结", "论是", "是什", "什么"):
        assert stop_tok in CJK_STOP_TOKENS
        assert stop_tok not in ev_toks
        assert f'"{stop_tok}"' not in expr
    assert ev_toks == ["共享", "享记", "记忆", "忆库", "adr", "03", "结论"]
    assert expr == '"共享" OR "享记" OR "记忆" OR "忆库" OR "adr" OR "03" OR "结论"'


def test_all_stop_tokens_fallback() -> None:
    expr, all_toks, ev_toks = segment_query("为什么")
    assert all_toks == ["为什", "什么"]
    assert ev_toks == ["为什", "什么"]
    assert expr == '"为什" OR "什么"'

    expr2, all_toks2, ev_toks2 = segment_query("什么")
    assert all_toks2 == ["什么"]
    assert ev_toks2 == ["什么"]
    assert expr2 == '"什么"'

    expr3, all_toks3, ev_toks3 = segment_query("的 什么")
    assert all_toks3 == ["的", "什么"]
    assert ev_toks3 == ["什么"]
    assert expr3 == '"什么"'
