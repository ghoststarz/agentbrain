"""记忆镜像写入必须 upsert：改一条记忆不能长出一份新的带时间戳副本。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from agentbrain.acl import AuthIdentity
from agentbrain.config import AppConfig
from agentbrain.parse import parse_note
from agentbrain.server import BrokerService

IDENT = AuthIdentity(client_id="lily", domain="agent", can_casual=False)
MIRROR_TAGS = ["hermes-mirror", "memory"]


def _load_plugin() -> ModuleType:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "obsidian_broker_mirror_probe",
        root / "integrations" / "hermes" / "obsidian-broker" / "__init__.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _facts(cfg: AppConfig) -> Path:
    return cfg.profile_dir / "20_LongTerm" / "Facts"


def test_mirror_write_upserts_in_place_instead_of_suffixing(isolated_env: AppConfig) -> None:
    svc = BrokerService(isolated_env)

    first = svc.write_note(
        {
            "content": "记忆条目 A：第一版正文。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "记忆条目 A",
            "upsert": True,
        },
        identity=IDENT,
    )
    path1 = first["path"]
    created1 = parse_note(isolated_env.profile_dir / path1, (isolated_env.profile_dir / path1).read_text(encoding="utf-8")).frontmatter["created"]

    # 同一标题再写一次（这次没有 previous_content，靠标题/文件名兜底）
    second = svc.write_note(
        {
            "content": "记忆条目 A：第二版正文，内容变长了。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "记忆条目 A",
            "upsert": True,
        },
        identity=IDENT,
    )
    assert second["path"] == path1, "同一条镜像笔记必须原地更新，不许另存副本"
    assert len(list(_facts(isolated_env).glob("*.md"))) == 1

    # 第三次：标题变了（编辑改了开头），但带 previous_content → 仍要命中同一条
    third = svc.write_note(
        {
            "content": "记忆条目 A 改名后：第三版正文。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "记忆条目 A 改名后",
            "upsert": True,
            "previous_content": "记忆条目 A：第二版正文，内容变长了。",
        },
        identity=IDENT,
    )
    assert third["path"] == path1
    assert len(list(_facts(isolated_env).glob("*.md"))) == 1

    text = (isolated_env.profile_dir / path1).read_text(encoding="utf-8")
    meta = parse_note(isolated_env.profile_dir / path1, text).frontmatter
    assert "第三版正文" in text
    assert "第二版正文" not in text
    assert meta["created"] == created1, "原地更新要保留原始的 created"
    assert meta["mirror_hash"], "镜像笔记要写 mirror_hash，供下次编辑精确命中"


def test_mirror_write_matches_by_hash_even_without_previous_content(isolated_env: AppConfig) -> None:
    svc = BrokerService(isolated_env)
    first = svc.write_note(
        {"content": "哈希命中探针正文。", "type": "fact", "tags": MIRROR_TAGS, "title": "哈希探针", "upsert": True},
        identity=IDENT,
    )
    # 模拟「只有旧正文、标题完全不同」的第二轮：靠 mirror_hash 找回来
    second = svc.write_note(
        {
            "content": "哈希命中探针正文（改）。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "另一个完全不同的标题",
            "upsert": True,
            "previous_content": "哈希命中探针正文。",
        },
        identity=IDENT,
    )
    assert second["path"] == first["path"]
    assert len(list(_facts(isolated_env).glob("*.md"))) == 1


def test_non_upsert_write_keeps_legacy_suffix_behavior(isolated_env: AppConfig) -> None:
    """不带 upsert 的写入仍是「新建」语义（普通笔记写入没有被改动）。"""
    svc = BrokerService(isolated_env)
    body = {"content": "普通笔记正文。", "type": "fact", "title": "同名笔记"}
    a = svc.write_note(dict(body), identity=IDENT)
    b = svc.write_note(dict(body), identity=IDENT)
    assert a["path"] != b["path"]
    assert len(list(_facts(isolated_env).glob("同名笔记*.md"))) == 2


def test_mirror_upsert_never_overwrites_handwritten_notes(isolated_env: AppConfig) -> None:
    """镜像写入只覆盖带 hermes-mirror 标签的笔记，手写笔记必须原样保留。"""
    svc = BrokerService(isolated_env)
    hand = _facts(isolated_env) / "手写笔记.md"
    hand.parent.mkdir(parents=True, exist_ok=True)
    hand.write_text(
        "---\ntype: fact\ncreated: 2026-01-01\nstatus: active\n---\n\n# 手写笔记\n\n我手写的原始内容。\n",
        encoding="utf-8",
    )
    svc.write_note(
        {
            "content": "我手写的原始内容。",
            "type": "fact",
            "tags": MIRROR_TAGS,
            "title": "手写笔记",
            "upsert": True,
            "previous_content": "我手写的原始内容。",
        },
        identity=IDENT,
    )
    assert "我手写的原始内容" in hand.read_text(encoding="utf-8")


def test_plugin_mirror_payload_requests_upsert_and_short_title() -> None:
    plugin = _load_plugin()
    assert plugin._derive_mirror_title("很长的记忆条目：后面还有很多细节不该进文件名") == "很长的记忆条目"
    assert len(plugin._derive_mirror_title("没有标点的超长记忆条目" * 10)) <= 24

    captured: dict[str, Any] = {}

    provider = object.__new__(plugin.ObsidianBrokerMemoryProvider)
    provider._closed = False
    provider._logger = __import__("logging").getLogger("probe")

    def fake_http(method: str, path: str, payload: dict[str, Any], timeout: float = 0) -> dict[str, Any]:
        captured.update(payload)
        return {"ok": True}

    provider._http_json = fake_http  # type: ignore[method-assign]
    provider.on_memory_write(
        "replace",
        "memory",
        "记忆条目 B：新的完整正文。",
        metadata={"previous_content": "记忆条目 B：旧的完整正文。"},
    )
    assert captured["upsert"] is True
    assert captured["previous_content"] == "记忆条目 B：旧的完整正文。"
    assert captured["title"] == "记忆条目 B"
    assert captured["tags"] == MIRROR_TAGS
