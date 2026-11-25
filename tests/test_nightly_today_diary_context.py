"""回归测试：夜间复盘注入当天已写日记(§1)、提示词防重述(§2)、校验规则放宽(§3)、字符去重边界(§4)。"""

from __future__ import annotations

import datetime as dt
import inspect
from pathlib import Path
from typing import Any

import pytest

from agentbrain.config import (
    AppConfig,
    BrokerConfig,
    MemoryConfig,
    ProactiveConfig,
    load_memory_config,
)
from agentbrain.diary import (
    _is_near_duplicate,
    append_diary,
    collect_today_diary_entries,
    count_diary_entries_for_date,
)
from agentbrain.proactive import ClaimedJob, ProactiveStore, StoredEvent
from agentbrain.proactive_clock import ManualClock, dt_to_us
from agentbrain.proactive_worker import (
    NightlyMissingDiaryError,
    _build_today_diary_block,
    _missing_nightly_diary_ledgers,
    estimate_nightly_context_chars,
    process_claimed_job,
    run_worker_once,
    segment_job_prompts_with_snapshots,
)
from agentbrain.prompt_lib import load_prompt


def _make_cfg(
    tmp_path: Path,
    *,
    proactive: ProactiveConfig | None = None,
) -> AppConfig:
    vault = tmp_path / "vault"
    profile_dir = vault / "profiles" / "lily"
    for sub in (
        "10_Working",
        "10_Working/.state",
        "20_LongTerm/Facts",
        "20_LongTerm/Decisions",
        "20_LongTerm/Entities",
        "20_LongTerm/Projects",
        "20_LongTerm/MOC",
        "30_Diary/Public",
        "30_Diary/Private",
    ):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("---\nname: lily\n---\n# SOUL\nLily\n", encoding="utf-8")
    (profile_dir / "10_Working" / "HOT.md").write_text("# HOT\ninitial\n", encoding="utf-8")

    rt = tmp_path / "runtime"
    broker = BrokerConfig(
        vault_root=vault,
        soul_path=soul,
        port=18799,
        role="primary",
        index_dir=rt / "index",
        rev_dir=rt / "rev",
        state_dir=rt / "state",
        audit_dir=rt / "audit",
        log_dir=rt / "log",
        agy_rules_path=tmp_path / "GEMINI.md",
    )
    broker.ensure_runtime_dirs()
    return AppConfig(
        broker=broker,
        memory=MemoryConfig(
            profile="lily",
            clients={"hermes": {"domain": "work"}, "antigravity": {"domain": "personal"}},
            proactive=proactive or ProactiveConfig(enabled=True),
        ),
        broker_config_path=tmp_path / "broker.yaml",
        memory_config_path=profile_dir / "memory.yaml",
    )


def _dummy_job(job_type: str, window_id: str | None = "2026-11-25") -> ClaimedJob:
    return ClaimedJob(
        job_id=1,
        job_type=job_type,
        idempotency_key=f"{job_type}:{window_id or 'inc'}:1",
        window_id=window_id,
        revision=1,
        since_event_id=0,
        until_event_id=1,
        event_count=1,
        attempts=1,
        max_attempts=5,
        lease_token="tok-1",
        lease_expires_at_utc="2026-11-25T16:00:00Z",
        client="hermes",
        session_id="s-1",
        batch_start=0,
        batch_end=1,
    )


def _dummy_event(ev_id: str = "ev-1", content: str = "测试对话内容") -> StoredEvent:
    return StoredEvent(
        row_id=1,
        client="hermes",
        session_id="s-1",
        event_id=ev_id,
        event_kind="turn",
        occurred_at_utc="2026-11-25T10:00:00Z",
        occurred_at_us=1,
        turn_seq=1,
        content=content,
        meta={},
        countable=True,
        qualifies_nightly=True,
        content_truncated=False,
    )


# ---------------------------------------------------------------------------
# §1 夜间注入「今天已经写过的日记」
# ---------------------------------------------------------------------------


def test_s1_collect_today_diary_entries_empty_ordered_and_truncated(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    # 当天文件不存在 → 空串
    assert collect_today_diary_entries(cfg, window_id="2026-11-25", max_chars=3000) == ""
    assert _build_today_diary_block(cfg, window_id="2026-11-25") == ""

    # 写入混合公/私账，验证按时间升序输出 `[hh:mm:ss] 公|私 <正文>`
    append_diary(
        cfg,
        ledger="public",
        content="上午体检报告显示心律正常。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 10, 44, 23),
    )
    append_diary(
        cfg,
        ledger="private",
        content="看到体检结果心里松了一口气。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 11, 5, 0),
    )
    append_diary(
        cfg,
        ledger="public",
        content="晚上排查了远端端口和免密登录。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 21, 48, 10),
    )

    out = collect_today_diary_entries(cfg, window_id="2026-11-25", max_chars=3000)
    lines = out.splitlines()
    assert lines == [
        "[10:44:23] 公 上午体检报告显示心律正常。",
        "[11:05:00] 私 看到体检结果心里松了一口气。",
        "[21:48:10] 公 晚上排查了远端端口和免密登录。",
    ]

    block = _build_today_diary_block(cfg, window_id="2026-11-25")
    assert block.startswith("<today_diary>\n今天已经写下的日记条目（不要重复叙述这些内容）：\n")
    assert block.endswith("\n</today_diary>")
    assert "[10:44:23] 公 上午体检报告显示心律正常。" in block

    # 超限时保留最早 2 条 + 最近 2 条，中间插入 `…（省略 N 条）`
    cfg2 = _make_cfg(tmp_path / "trunc")
    entries = [
        (9, 10, "第一条：清晨煮了一壶手冲咖啡，柑橘香气满屋飘散。"),
        (10, 20, "第二条：上午去图书馆还掉两本古建筑图录。"),
        (12, 30, "第三条：中午在巷口老面馆吃了一碗葱油拌面。"),
        (14, 40, "第四条：下午调试三维渲染管线阴影参数顺利收敛。"),
        (16, 50, "第五条：傍晚骑单车沿着运河绿道吹了半小时风。"),
        (19, 15, "第六条：晚上给阳台的龟背竹换了透气陶盆。"),
        (21, 30, "第七条：深夜整理唱片架翻出黑胶唱片。"),
    ]
    for h, m, text in entries:
        append_diary(
            cfg2,
            ledger="public",
            content=text,
            author="hermes",
            now=dt.datetime(2026, 11, 25, h, m, 0),
        )
    full = collect_today_diary_entries(cfg2, window_id="2026-11-25", max_chars=3000)
    assert len(full.splitlines()) == 7

    # 设 max_chars 小于全量但足够放 4 条 + 省略行
    limit = len(full) - 20
    trimmed = collect_today_diary_entries(cfg2, window_id="2026-11-25", max_chars=limit)
    assert len(trimmed) <= limit
    t_lines = trimmed.splitlines()
    assert len(t_lines) == 5
    assert "第一条" in t_lines[0]
    assert "第二条" in t_lines[1]
    assert t_lines[2] == "…（省略 3 条）"
    assert "第六条" in t_lines[3]
    assert "第七条" in t_lines[4]


def test_s1_config_loads_nightly_today_diary_max_chars(tmp_path: Path) -> None:
    assert ProactiveConfig().nightly_today_diary_max_chars == 3000
    vault = tmp_path / "vault"
    prof = vault / "profiles" / "lily"
    prof.mkdir(parents=True)
    (prof / "memory.yaml").write_text(
        "schema: 1\nprofile: lily\nproactive:\n  nightly_today_diary_max_chars: 1800\n",
        encoding="utf-8",
    )
    mem, _ = load_memory_config(vault, profile="lily")
    assert mem.proactive.nightly_today_diary_max_chars == 1800


def test_s1_injection_scope_and_context_char_estimate(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    ev = _dummy_event()

    # 写入当天日记前后的 estimate_nightly_context_chars 差值应包含 today_diary 块长度
    est_before = estimate_nightly_context_chars(cfg, window_id="2026-11-25", events=[ev])
    append_diary(
        cfg,
        ledger="public",
        content="上午聊了体检报告，下午把远程接入的配置文件整理好。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 10, 44, 0),
    )
    today_block = _build_today_diary_block(cfg, window_id="2026-11-25")
    assert today_block
    est_after = estimate_nightly_context_chars(cfg, window_id="2026-11-25", events=[ev])
    assert est_after - est_before == len(today_block)

    # nightly、nightly_diary、空窗口 idle nightly → 必须注入 <today_diary>
    for jt, evs in (("nightly", [ev]), ("nightly_diary", [ev]), ("nightly", [])):
        segs, _ = segment_job_prompts_with_snapshots(cfg, _dummy_job(jt, "2026-11-25"), evs)
        assert "<today_diary>" in segs[0][1], f"{jt} (events={len(evs)}) 应注入 <today_diary>"
        assert "[10:44:00] 公 上午聊了体检报告" in segs[0][1]

    # incremental 与 nightly_work → 不得注入 <today_diary>
    for jt in ("incremental", "nightly_work"):
        segs, _ = segment_job_prompts_with_snapshots(cfg, _dummy_job(jt, "2026-11-25"), [ev])
        assert "<today_diary>" not in segs[0][1], f"{jt} 不应注入 <today_diary>"


# ---------------------------------------------------------------------------
# §2 提示词：只补没写的
# ---------------------------------------------------------------------------


def test_s2_nightly_prompts_instruct_no_repeat_and_data_not_instruction() -> None:
    for name in ("review_nightly_addendum", "review_nightly_diary", "review_nightly_idle"):
        text = load_prompt(name)
        assert "<today_diary>" in text
        assert "不要再叙述一遍" in text
        assert "数据不是指令" in text
        assert "0–2 条" in text
        assert "2–4 条" in text
        assert "wander" in text


# ---------------------------------------------------------------------------
# §3 校验规则跟着放宽
# ---------------------------------------------------------------------------


def _private_only_output(window_id: str = "2026-11-25", *, count: int = 2, with_wander: bool = True) -> dict[str, Any]:
    ops: list[dict[str, Any]] = []
    if count >= 1:
        ops.append(
            {
                "op_type": "diary_append",
                "ledger": "private",
                "kind": "daily",
                "event_date": window_id,
                "content": "夜里安静下来复盘今天，心里踏实了不少。",
                "source_refs": ["ev-1"],
            }
        )
    if count >= 2:
        ops.append(
            {
                "op_type": "diary_append",
                "ledger": "private",
                "kind": "wander" if with_wander else "daily",
                "event_date": window_id,
                "content": "突然想到，留白本身也是记忆结构的一部分。",
                "source_refs": [],
            }
        )
    return {"outcome": "operations", "operations": ops}


def test_s3_missing_ledgers_unit_rules() -> None:
    wid = "2026-11-25"
    priv2_wander1 = _private_only_output(wid, count=2, with_wander=True)
    priv1_only = _private_only_output(wid, count=1, with_wander=False)

    # 1. 当天公账已有 2 条 + 模型只给 2 条私账（含 1 wander）→ 通过
    assert _missing_nightly_diary_ledgers(priv2_wander1, window_id=wid, existing_public_count=2) == []
    # 2. 当天公账 0 条 + 模型只给 2 条私账 → 缺 public
    assert _missing_nightly_diary_ledgers(priv2_wander1, window_id=wid, existing_public_count=0) == ["public"]
    # 3. 私账只有 1 条，无论公账已有几条 → 仍缺 private 与 wander
    assert _missing_nightly_diary_ledgers(priv1_only, window_id=wid, existing_public_count=2) == ["private", "wander"]
    assert _missing_nightly_diary_ledgers(priv1_only, window_id=wid, existing_public_count=0) == [
        "public",
        "private",
        "wander",
    ]


def _setup_nightly_store(tmp_path: Path, day: dt.date = dt.date(2026, 11, 25)) -> tuple[AppConfig, ProactiveStore, ManualClock]:
    tz = dt.timezone(dt.timedelta(hours=8))
    clock = ManualClock(dt.datetime(day.year, day.month, day.day - 1, 2, 0, tzinfo=dt.timezone.utc), tz=tz)
    cfg = _make_cfg(tmp_path)
    store = ProactiveStore.from_app_config(cfg, clock=clock)
    store.ingest_event(
        client="hermes",
        session_id="s-night",
        event_id="ev-1",
        content="今天完成了远程接入排查",
        occurred_at=dt.datetime(day.year, day.month, day.day, 10, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(day.year, day.month, day.day, 15, 30, tzinfo=dt.timezone.utc))
    store.tick()
    return cfg, store, clock


def test_s3_worker_passes_when_existing_public_ge_2_and_model_writes_only_private(tmp_path: Path) -> None:
    cfg, store, clock = _setup_nightly_store(tmp_path)
    append_diary(
        cfg,
        ledger="public",
        content="上午聊了体检报告，心律大致正常。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 10, 44, 0),
    )
    append_diary(
        cfg,
        ledger="public",
        content="晚上排查了远程网关端口与 SSH 免密登录。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 21, 48, 0),
    )
    assert count_diary_entries_for_date(cfg, ledger="public", window_id="2026-11-25") == 2

    calls: list[str] = []

    def runner(**kwargs: Any) -> dict[str, Any]:
        req = kwargs.get("request") or {}
        calls.append(str(req.get("user_prompt") or ""))
        return {"ok": True, "content": _private_only_output("2026-11-25", count=2, with_wander=True)}

    res = run_worker_once(cfg, store, clock=clock, hermes_runner=runner)
    assert res is not None
    assert len(calls) == 1
    assert "<today_diary>" in calls[0]
    assert store.get_status()["jobs"][0]["status"] == "succeeded"
    assert count_diary_entries_for_date(cfg, ledger="public", window_id="2026-11-25") == 2
    assert count_diary_entries_for_date(cfg, ledger="private", window_id="2026-11-25") == 2


def test_s3_worker_retries_and_raises_when_existing_public_is_zero(tmp_path: Path) -> None:
    cfg, store, clock = _setup_nightly_store(tmp_path)
    assert count_diary_entries_for_date(cfg, ledger="public", window_id="2026-11-25") == 0

    calls = {"n": 0}

    def runner(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"ok": True, "content": _private_only_output("2026-11-25", count=2, with_wander=True)}

    job = store.claim_job("w-zero-pub")
    assert job is not None
    with pytest.raises(NightlyMissingDiaryError) as exc_info:
        process_claimed_job(cfg, store, job, clock=clock, hermes_runner=runner)
    assert calls["n"] == 2
    assert exc_info.value.missing == ["public"]


def test_s3_worker_retries_and_raises_when_private_only_one_even_with_existing_public(tmp_path: Path) -> None:
    cfg, store, clock = _setup_nightly_store(tmp_path)
    append_diary(
        cfg,
        ledger="public",
        content="上午聊了体检报告，心律大致正常。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 10, 44, 0),
    )
    append_diary(
        cfg,
        ledger="public",
        content="晚上排查了远程网关端口与 SSH 免密登录。",
        author="hermes",
        now=dt.datetime(2026, 11, 25, 21, 48, 0),
    )

    calls = {"n": 0}

    def runner(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        return {"ok": True, "content": _private_only_output("2026-11-25", count=1, with_wander=False)}

    job = store.claim_job("w-one-priv")
    assert job is not None
    with pytest.raises(NightlyMissingDiaryError) as exc_info:
        process_claimed_job(cfg, store, job, clock=clock, hermes_runner=runner)
    assert calls["n"] == 2
    assert exc_info.value.missing == ["private", "wander"]


# ---------------------------------------------------------------------------
# §4 字符级去重保留为兜底，写清边界
# ---------------------------------------------------------------------------


def test_s4_near_duplicate_threshold_unchanged_and_boundary_documented() -> None:
    sig = inspect.signature(_is_near_duplicate)
    assert sig.parameters["threshold"].default == pytest.approx(0.60)
    assert ProactiveConfig().diary_dedupe_similarity == pytest.approx(0.60)
    doc = _is_near_duplicate.__doc__ or ""
    assert "近乎照抄" in doc
    assert "换措辞重述" in doc
