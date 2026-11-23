"""冲突裁决器、notices 队列、服务端 /v1/notices/ack、Hermes provider 与同步脚本测试。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentbrain.cli import main as cli_main
from agentbrain.config import AppConfig
from agentbrain.conflicts import (
    MANAGED_END,
    MANAGED_START,
    ack_notices,
    append_notice,
    has_conflict_markers,
    list_pending_notices,
    resolve_conflicts,
)
from agentbrain.server import BrokerService


def _conflict_block(remote: str, local: str) -> str:
    return f"<<<<<<< HEAD\n{remote}\n=======\n{local}\n>>>>>>> local-commit\n"


# ---------------------------------------------------------------------------
# 六条裁决规则 + 硬要求
# ---------------------------------------------------------------------------


def test_rule1_diary_union_sort_dedupe(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    diary = vault / "profiles" / "lily" / "30_Diary" / "Public" / "2026-11-23.md"
    diary.parent.mkdir(parents=True, exist_ok=True)
    diary.write_text(
        "# 2026-11-23 (public)\n\n"
        + _conflict_block(
            "- [09:00:00] (hermes) 共同早安\n- [14:30:00] (hermes) 远端下午",
            "- [09:00:00] (hermes) 共同早安\n- [10:15:00] (antigravity) 本地上午",
        ),
        encoding="utf-8",
    )

    notices_file = tmp_path / "notices.jsonl"
    res = resolve_conflicts(vault, notices_path=notices_file)
    assert len(res["resolved"]) == 1
    assert res["resolved"][0]["rule"] == "diary_union_sort"
    assert res["unresolved"] == []
    assert len(res["notices"]) == 1
    assert res["notices"][0]["kind"] == "conflict"
    assert len(res["notices"][0]["text"]) <= 120

    out = diary.read_text(encoding="utf-8")
    assert not has_conflict_markers(out)
    lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
    assert lines == [
        "- [09:00:00] (hermes) 共同早安",
        "- [10:15:00] (antigravity) 本地上午",
        "- [14:30:00] (hermes) 远端下午",
    ]


def test_rule2_handoff_json_merge_newer_wins(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    hf = vault / "profiles" / "lily" / "10_Working" / ".state" / "handoff.json"
    hf.parent.mkdir(parents=True, exist_ok=True)

    remote_json = json.dumps(
        {
            "tasks": {
                "shared-a": {"task_key": "shared-a", "status": "open", "summary": "远端旧", "updated_at": "2026-11-23T08:00:00+00:00"},
                "shared-b": {"task_key": "shared-b", "status": "done", "summary": "远端新", "updated_at": "2026-11-23T12:00:00+00:00"},
                "remote-only": {"task_key": "remote-only", "status": "open", "summary": "远端独有", "updated_at": "2026-11-23T09:00:00+00:00"},
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    local_json = json.dumps(
        {
            "tasks": {
                "shared-a": {"task_key": "shared-a", "status": "in_progress", "summary": "本地新", "updated_at": "2026-11-23T11:00:00+00:00"},
                "shared-b": {"task_key": "shared-b", "status": "open", "summary": "本地旧", "updated_at": "2026-11-23T10:00:00+00:00"},
                "local-only": {"task_key": "local-only", "status": "open", "summary": "本地独有", "updated_at": "2026-11-23T09:30:00+00:00"},
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    hf.write_text(_conflict_block(remote_json, local_json), encoding="utf-8")

    res = resolve_conflicts(vault, write_notices=False)
    assert len(res["resolved"]) == 1
    assert res["resolved"][0]["rule"] == "handoff_json_merge"
    assert res["unresolved"] == []

    raw = hf.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not has_conflict_markers(raw)
    data = json.loads(raw)
    assert data["tasks"]["shared-a"]["summary"] == "本地新"
    assert data["tasks"]["shared-b"]["summary"] == "远端新"
    assert "remote-only" in data["tasks"]
    assert "local-only" in data["tasks"]


def test_rule3_hot_md_paragraph_union_truncate_and_no_comments(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    pdir = vault / "profiles" / "lily"
    (pdir / "10_Working").mkdir(parents=True, exist_ok=True)
    (pdir / "memory.yaml").write_text("schema: 1\nprofile: lily\nworking:\n  hot_max_chars: 45\n", encoding="utf-8")

    hot = pdir / "10_Working" / "HOT.md"
    hot.write_text(
        _conflict_block(
            "远端第一段内容\n\n共同段落",
            "共同段落\n\n本地独有第二段\n\n本地超长尾部段落一定会超出四十五字上限而被截断掉",
        ),
        encoding="utf-8",
    )

    res = resolve_conflicts(vault, write_notices=False)
    assert len(res["resolved"]) == 1
    assert res["resolved"][0]["rule"] == "hot_md_paragraph_union"
    assert res["unresolved"] == []

    out = hot.read_text(encoding="utf-8")
    assert not has_conflict_markers(out)
    assert "<!--" not in out
    assert len(out.rstrip("\n")) <= 45
    assert out.startswith("远端第一段内容\n\n共同段落")
    assert "本地独有第二段" in out
    assert "本地超长尾部段落" not in out


def test_rule4_longterm_add_add_renumber_and_three_digit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    facts_dir = vault / "profiles" / "lily" / "20_LongTerm" / "Facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    # 放一个 FACT-99 测试 99 -> 100 三位编号规则
    (facts_dir / "FACT-99-历史事实.md").write_text("---\ntitle: 历史\n---\n# 历史\n", encoding="utf-8")

    conflict_file = facts_dir / "FACT-07-同步冲突.md"
    remote_doc = "---\ntitle: 远端标题\nsummary: 远端摘要\n---\n# 远端标题\n\n远端正文"
    local_doc = "---\ntitle: 本地标题\nsummary: 本地摘要\n---\n# 本地标题\n\n本地正文"
    conflict_file.write_text(_conflict_block(remote_doc, local_doc), encoding="utf-8")

    res = resolve_conflicts(vault, write_notices=False)
    assert len(res["resolved"]) == 1
    assert res["resolved"][0]["rule"] == "longterm_add_add_renumber"
    assert res["unresolved"] == []

    # 原名保留远端
    assert conflict_file.read_text(encoding="utf-8").strip() == remote_doc
    # 本地改存为 FACT-100-...，保留本地 frontmatter title/summary
    new_file = facts_dir / "FACT-100-同步冲突.md"
    assert new_file.exists()
    new_text = new_file.read_text(encoding="utf-8")
    assert "title: 本地标题" in new_text
    assert "summary: 本地摘要" in new_text
    assert "本地正文" in new_text
    assert not has_conflict_markers(new_text)


def test_rule5_longterm_section_merge_preserves_unique_managed_markers(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proj = vault / "profiles" / "lily" / "20_LongTerm" / "Projects" / "PROJ-01-AgentBrain.md"
    proj.parent.mkdir(parents=True, exist_ok=True)

    remote_body = (
        f"---\ntitle: AgentBrain\n---\n# AgentBrain\n\n{MANAGED_START}\n"
        "### 共同小节\n远端版本内容\n\n### 远端独有\n远端独有内容\n"
        f"{MANAGED_END}"
    )
    local_body = (
        f"---\ntitle: AgentBrain\n---\n# AgentBrain\n\n{MANAGED_START}\n"
        "### 共同小节\n本地旧版本内容\n\n### 本地独有\n本地独有内容\n"
        f"{MANAGED_END}"
    )
    proj.write_text(_conflict_block(remote_body, local_body), encoding="utf-8")

    res = resolve_conflicts(vault, write_notices=False)
    assert len(res["resolved"]) == 1
    assert res["resolved"][0]["rule"] == "longterm_section_merge"
    assert res["unresolved"] == []

    out = proj.read_text(encoding="utf-8")
    assert not has_conflict_markers(out)
    assert out.count(MANAGED_START) == 1
    assert out.count(MANAGED_END) == 1
    assert out.find(MANAGED_START) < out.find(MANAGED_END)
    assert "远端版本内容" in out
    assert "本地旧版本内容" not in out
    assert "### 远端独有" in out
    assert "### 本地独有" in out


def test_rule6_fallback_archive_unresolved(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    misc = vault / "profiles" / "lily" / "misc" / "custom.txt"
    misc.parent.mkdir(parents=True, exist_ok=True)
    misc.write_text(_conflict_block("远端文本", "本地完整文本"), encoding="utf-8")

    fixed_now = dt.datetime(2026, 11, 23, 16, 30, 0)
    notices_file = tmp_path / "notices.jsonl"
    res = resolve_conflicts(vault, notices_path=notices_file, now=fixed_now)
    assert res["resolved"] == []
    assert len(res["unresolved"]) == 1
    assert res["unresolved"][0]["rule"] == "archive_unresolved"
    assert len(res["notices"]) == 1
    assert res["notices"][0]["kind"] == "severe"

    assert misc.read_text(encoding="utf-8").strip() == "远端文本"
    archived = vault / "90_Archive" / "_conflicts" / "20261123-163000" / "profiles" / "lily" / "misc" / "custom.txt"
    assert archived.exists()
    assert archived.read_text(encoding="utf-8").strip() == "本地完整文本"


def test_idempotent_and_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    hot = vault / "profiles" / "lily" / "10_Working" / "HOT.md"
    hot.parent.mkdir(parents=True, exist_ok=True)
    original = _conflict_block("远端段", "本地段")
    hot.write_text(original, encoding="utf-8")
    notices_file = tmp_path / "notices.jsonl"

    # --dry-run 不写盘、不写 notices
    rc = cli_main(
        [
            "resolve-conflicts",
            "--vault",
            str(vault),
            "--dry-run",
            "--notices-path",
            str(notices_file),
        ]
    )
    assert rc == 0
    dry_out = json.loads(capsys.readouterr().out)
    assert len(dry_out["resolved"]) == 1
    assert hot.read_text(encoding="utf-8") == original
    assert not notices_file.exists()

    # 第一次真实执行
    rc1 = cli_main(["resolve-conflicts", "--vault", str(vault), "--notices-path", str(notices_file)])
    assert rc1 == 0
    out1 = json.loads(capsys.readouterr().out)
    assert len(out1["resolved"]) == 1
    assert notices_file.exists()

    # 第二次执行幂等：resolved 为空
    rc2 = cli_main(["resolve-conflicts", "--vault", str(vault), "--notices-path", str(notices_file)])
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["resolved"] == []
    assert out2["unresolved"] == []
    assert out2["notices"] == []


# ---------------------------------------------------------------------------
# M2-b：服务端 /v1/search notices + /v1/notices/ack + Hermes provider
# ---------------------------------------------------------------------------


def test_server_search_notices_and_ack(isolated_env: AppConfig) -> None:
    svc = BrokerService(isolated_env)
    notices_path = isolated_env.broker.state_dir / "notices.jsonl"

    # 写入 4 条未投递 notice，验证 /v1/search 最多返回 3 条
    ids: list[str] = []
    for i in range(4):
        entry = append_notice(notices_path, kind="conflict" if i < 3 else "severe", text=f"测试冲突回执 {i}")
        ids.append(entry["id"])

    res1 = svc.search({"query": "任意查询", "session_id": "sess-n1"})
    assert "notices" in res1
    assert len(res1["notices"]) == 3
    assert [n["id"] for n in res1["notices"]] == ids[:3]

    # ack 前 3 条
    ack_res = svc.notices_ack({"ids": ids[:3]})
    assert ack_res == {"ok": True, "acked": 3}

    # 下一次 search 只剩第 4 条
    res2 = svc.search({"query": "任意查询"})
    assert len(res2["notices"]) == 1
    assert res2["notices"][0]["id"] == ids[3]
    assert res2["notices"][0]["kind"] == "severe"

    # ack 第 4 条后不再返回 notices 键
    assert svc.notices_ack({"ids": [ids[3]]}) == {"ok": True, "acked": 1}
    res3 = svc.search({"query": "任意查询"})
    assert "notices" not in res3
    assert list_pending_notices(notices_path) == []
    assert ack_notices(notices_path, ids) == 0


def _load_hermes_provider_module() -> Any:
    plugin_path = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "agentbrain" / "__init__.py"
    spec = importlib.util.spec_from_file_location("agentbrain_hermes_provider_test", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hermes_provider_notices_gateway_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_hermes_provider_module()
    provider = mod.ObsidianBrokerMemoryProvider()

    # 1) 网关可用时走 _publish_review_notice 后 ack，不注入上下文
    published_msgs: list[tuple[str, str]] = []
    acked_payloads: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mod,
        "_publish_review_notice",
        lambda sid, text: (published_msgs.append((sid, text)) or True),
    )

    def fake_http_gateway(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {
                "hits": [],
                "revision": 2,
                "notices": [{"id": "n1", "kind": "conflict", "text": "同步冲突已自动裁决 1 个文件"}],
            }
        if path == "/v1/notices/ack":
            acked_payloads.append(dict(payload or {}))
            return {"ok": True, "acked": 1}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http_gateway)
    out1 = provider.prefetch("测试问题", session_id="sess-gw")
    assert out1 == ""
    assert len(published_msgs) == 1
    assert published_msgs[0][0] == "sess-gw"
    assert "同步冲突已自动裁决" in published_msgs[0][1]
    assert acked_payloads == [{"ids": ["n1"]}]

    # 2) 取不到网关时退化为上下文注入并 ack，且同 id 不重复注入
    monkeypatch.setattr(mod, "_publish_review_notice", lambda sid, text: False)
    acked_payloads.clear()

    def fake_http_fallback(method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 1.5) -> dict[str, Any]:
        if path == "/v1/search":
            return {
                "hits": [],
                "revision": 3,
                "notices": [{"id": "n2", "kind": "severe", "text": "同步冲突需处理：1 个文件未解决"}],
            }
        if path == "/v1/notices/ack":
            acked_payloads.append(dict(payload or {}))
            return {"ok": True, "acked": 1}
        return {}

    monkeypatch.setattr(provider, "_http_json", fake_http_fallback)
    out2 = provider.prefetch("再问一次", session_id="sess-nogw")
    assert "[SYNC_NOTICE]" in out2
    assert "同步冲突需处理" in out2
    assert acked_payloads == [{"ids": ["n2"]}]

    # 即使服务端再次返回同 id，本地去重也不重复注入
    out3 = provider.prefetch("第三次", session_id="sess-nogw")
    assert out3 == ""


# ---------------------------------------------------------------------------
# M2-c：memory-sync.ps1 编码（UTF-8 with BOM）与 PowerShell 5.1 语法检查
# ---------------------------------------------------------------------------


def test_memory_sync_ps1_utf8_bom_and_syntax() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "memory-sync.ps1"
    raw = script.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "memory-sync.ps1 必须保持 UTF-8 with BOM"
    text = raw.decode("utf-8-sig")
    assert "git fetch" in text
    assert "pull --rebase" in text
    assert "resolve-conflicts" in text
    assert "reindex" in text
    assert "*/30_Diary/** merge=union" in text

    # 用 PowerShell 5.1 内置 AST Parser 校验零语法错误
    ps_cmd = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"PowerShell 语法校验失败: {proc.stderr}"
