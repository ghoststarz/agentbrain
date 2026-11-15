"""CJK 重叠二元组与 ASCII 小写分词器。"""

from __future__ import annotations

import re

# 匹配连续的 CJK 汉字或连续的 ASCII 字母数字
_CJK_RANGE = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_TOKEN_RUN_RE = re.compile(rf"[{_CJK_RANGE}]+|[A-Za-z0-9]+")
_CJK_CHAR_RE = re.compile(rf"^[{_CJK_RANGE}]$")

# 查询侧中文功能词停用表：仅在 segment_query() 中过滤，不影响索引分词
CJK_STOP_TOKENS: frozenset[str] = frozenset({
    # 代词 / 指代
    "我们", "你们", "他们", "它们", "它的", "他们的", "们的", "这个", "那个", "这些", "那些",
    # 数量 / 存在
    "一条", "一些", "一个", "有一", "里有", "库里", "的共",
    # 疑问 / 判断
    "什么", "是什", "为什", "怎么", "是否", "如何", "可以", "能不", "就是", "不是", "还是",
    # 连词 / 时间
    "但是", "因为", "所以", "而且", "然后", "如果", "时候", "已经", "现在",
    # 助词碎片
    "的结", "论是", "的了", "了吗", "的话",
})


def is_single_cjk_token(token: str) -> bool:
    """判断 token 是否为单字 CJK 片段（保留成 token 但不计入证据）。"""
    return len(token) == 1 and bool(_CJK_CHAR_RE.match(token))


def is_evidence_token(token: str) -> bool:
    """判断 token 是否允许计入检索证据分数。"""
    if not token:
        return False
    return not is_single_cjk_token(token)


def segment_text(text: str) -> list[str]:
    """将文本切分为 CJK 重叠二元组与小写 ASCII token 列表。

    - 长度 >= 2 的连续 CJK 片段切分为重叠二元组；
    - 长度为 1 的 CJK 片段保留为单字 token（检索时不计入证据）；
    - ASCII 字母数字序列整体转小写后成 token。
    """
    tokens: list[str] = []
    for match in _TOKEN_RUN_RE.finditer(text):
        run = match.group(0)
        first = run[0]
        if first.isascii():
            tokens.append(run.lower())
        elif len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i : i + 2])
    return tokens


def segment_for_index(text: str) -> str:
    """生成写入 SQLite FTS5 的空格分隔分词串。"""
    return " ".join(segment_text(text))


def segment_query(query: str) -> tuple[str, list[str], list[str]]:
    """将查询串转为 FTS5 MATCH 表达式及 token 列表。

    返回:
        (fts_match_expr, all_tokens, evidence_tokens)
    """
    raw_tokens = segment_text(query)
    if not raw_tokens:
        return "", [], []

    # 去重并保持顺序
    all_tokens = list(dict.fromkeys(raw_tokens))
    raw_evidence_tokens = [t for t in all_tokens if is_evidence_token(t)]
    evidence_tokens = [t for t in raw_evidence_tokens if t not in CJK_STOP_TOKENS]
    if not evidence_tokens and raw_evidence_tokens:
        # 全为停用词时回退到未过滤的证据词，保持原行为
        evidence_tokens = raw_evidence_tokens
    if evidence_tokens:
        fts_expr = " OR ".join(f'"{t}"' for t in evidence_tokens)
    else:
        fts_expr = " AND ".join(f'"{t}"' for t in all_tokens)
    return fts_expr, all_tokens, evidence_tokens
