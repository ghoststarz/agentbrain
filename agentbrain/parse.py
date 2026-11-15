"""Markdown 笔记解析模块：Frontmatter 契约、全量 Wikilink 语法与类型化关系提取。"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

# 非 Markdown 附件扩展名，遇到 ![[图.png]] 时不建边
_NON_NOTE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    ".tiff",
    ".pdf",
    ".mp3",
    ".mp4",
    ".wav",
    ".ogg",
    ".mov",
    ".avi",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".csv",
    ".xlsx",
    ".docx",
    ".pptx",
}

# 匹配 ![[target]] 或 [[target]]
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]\r\n]+?)\]\]")
# 匹配普通 Markdown 链接 [text](target)，排除图片 ![alt](src)
_MDLINK_RE = re.compile(r"(?<!!)\[([^\[\]\r\n]*?)\]\(([^()\r\n]+?)\)")
# 匹配行首类型化关系，例如 "- requires [[X]]" 或 "* supports [[X]]"
_TYPED_REL_PREFIX_RE = re.compile(r"^\s*[-*]\s+([A-Za-z_][A-Za-z0-9_-]*)\s+$")


@dataclass(frozen=True)
class ParsedLink:
    """解析出的一条知识图谱边。"""

    dst_title: str
    anchor: str | None
    embed: int
    relation: str
    context_line: str


@dataclass
class ParsedNote:
    """解析后的完整笔记结构。"""

    path: Path
    title: str
    h1: str | None
    frontmatter: dict[str, Any]
    body: str
    chunks: list[str] = field(default_factory=list)
    links: list[ParsedLink] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    keys2: list[str] = field(default_factory=list)
    order: int = 100


def normalize_trigger_keys(val: Any) -> list[str]:
    """将 frontmatter 中的 keys / keys2 规范化为去重后的非空字符串列表。"""
    if val is None:
        return []
    raw_items = [val] if isinstance(val, str) else (val if isinstance(val, list) else [])
    seen: set[str] = set()
    result: list[str] = []
    for item in raw_items:
        if item is None:
            continue
        cleaned = str(item).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def normalize_trigger_order(val: Any, default: int = 100) -> int:
    """将 frontmatter 中的 order 规范化为整数（默认 100）。"""
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _normalize_yaml_value(val: Any) -> Any:
    """递归将 YAML 解析出的 date/datetime 转为 ISO 字符串，便于 JSON 序列化。"""
    if isinstance(val, (dt.date, dt.datetime)):
        return val.isoformat()
    if isinstance(val, list):
        return [_normalize_yaml_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _normalize_yaml_value(v) for k, v in val.items()}
    return val


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """分离 YAML Frontmatter 与 Markdown 正文。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized

    end_idx = normalized.find("\n---\n", 4)
    if end_idx == -1:
        # 检查文件是否以 "\n---" 结尾（无正文）
        if normalized.endswith("\n---"):
            raw_yaml = normalized[4:-4]
            body = ""
        else:
            return {}, normalized
    else:
        raw_yaml = normalized[4:end_idx]
        body = normalized[end_idx + 5 :]

    parsed = yaml.safe_load(raw_yaml)
    if not isinstance(parsed, dict):
        return {}, body
    return _normalize_yaml_value(parsed), body


def _parse_wikilink_target(inner: str) -> tuple[str, str | None] | None:
    """解析 [[笔记#小节|别名]] 中的目标标题与锚点。"""
    target_part = inner.split("|", 1)[0].strip()
    if not target_part:
        return None

    anchor: str | None = None
    if "#" in target_part:
        note_part, anchor_part = target_part.split("#", 1)
        note_part = note_part.strip()
        anchor = anchor_part.strip() or None
    else:
        note_part = target_part

    if not note_part:
        return None

    # 若带有路径，取最后一段 stem（去掉 .md 后缀，保留其它扩展名供过滤判断）
    suffix = Path(note_part).suffix.lower()
    if suffix in _NON_NOTE_EXTENSIONS:
        return None
    if suffix == ".md":
        note_part = note_part[:-3]
    else:
        # 若有路径分隔符如 folder/note，取最后一段
        if "/" in note_part or "\\" in note_part:
            note_part = Path(note_part).name

    return note_part, anchor


def extract_links(body: str) -> list[ParsedLink]:
    """从 Markdown 正文中提取所有双链、vault 内 .md 相对链接与类型化关系。"""
    links: list[ParsedLink] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue

        # 1. 提取 [[...]] 和 ![[...]]
        for m in _WIKILINK_RE.finditer(line):
            is_embed = m.group(1) == "!"
            parsed_target = _parse_wikilink_target(m.group(2))
            if parsed_target is None:
                continue
            dst_title, anchor = parsed_target

            relation = "links_to"
            if not is_embed:
                prefix = line[: m.start()]
                rel_match = _TYPED_REL_PREFIX_RE.match(prefix)
                if rel_match:
                    relation = rel_match.group(1).lower()

            links.append(
                ParsedLink(
                    dst_title=dst_title,
                    anchor=anchor,
                    embed=1 if is_embed else 0,
                    relation=relation,
                    context_line=line,
                )
            )

        # 2. 提取 [文字](相对.md)
        for m in _MDLINK_RE.finditer(line):
            href = m.group(2).strip()
            # 去掉可选 title，如 (foo.md "title")
            if " " in href and not href.startswith("<"):
                href = href.split(" ", 1)[0]
            href = href.strip("<>")
            lower_href = href.lower()
            if lower_href.startswith(("http://", "https://", "mailto:", "file://", "#")):
                continue

            anchor: str | None = None
            if "#" in href:
                path_part, anchor_part = href.split("#", 1)
                anchor = anchor_part.strip() or None
            else:
                path_part = href

            path_part = unquote(path_part.strip())
            if not path_part.lower().endswith(".md"):
                continue

            dst_title = Path(path_part).stem
            if not dst_title:
                continue

            links.append(
                ParsedLink(
                    dst_title=dst_title,
                    anchor=anchor,
                    embed=0,
                    relation="links_to",
                    context_line=line,
                )
            )

    return links


def extract_h1(body: str) -> str | None:
    """提取正文中的第一个一级标题。"""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def chunk_body(title: str, body: str, max_chunk_chars: int = 500) -> list[str]:
    """将笔记正文切分为适合索引与摘要展示的文本块。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if not paragraphs:
        return [title]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > max_chunk_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(para), max_chunk_chars):
                chunks.append(para[i : i + max_chunk_chars])
            continue

        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chunk_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    # 确保首块包含笔记文件名标题，以便标题词直接可检索
    if chunks and title not in chunks[0]:
        chunks[0] = f"{title}\n{chunks[0]}"
    return chunks


def parse_note(path: Path, text: str) -> ParsedNote:
    """解析单篇 Markdown 笔记（Frontmatter + 正文分块 + 双链）。"""
    fm, body = split_frontmatter(text)
    title = path.stem
    h1 = extract_h1(body)
    links = extract_links(body)
    chunks = chunk_body(title, body)
    keys = normalize_trigger_keys(fm.get("keys"))
    keys2 = normalize_trigger_keys(fm.get("keys2"))
    order = normalize_trigger_order(fm.get("order"), default=100)
    return ParsedNote(
        path=path,
        title=title,
        h1=h1,
        frontmatter=fm,
        body=body,
        chunks=chunks,
        links=links,
        keys=keys,
        keys2=keys2,
        order=order,
    )
