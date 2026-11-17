"""Stage 1 主动整理调度核心隔离测试（临时目录 + 注入时钟，不触碰真实 Vault/规则/模型）。"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest

from agentbrain.config import AppConfig, ProactiveConfig
from agentbrain.proactive import (
    EVENT_KIND_HUMAN_TURN,
    EVENT_KIND_REFERENCE_ONLY,
    JOB_STATUS_DEAD,
    JOB_STATUS_RETRY_WAIT,
    JOB_STATUS_SUCCEEDED,
    LeaseMismatchError,
    PayloadTooLargeError,
    ProactiveStore,
)
from agentbrain.proactive_clock import ManualClock


class SyntheticDSTZone(dt.tzinfo):
    """合成标准库 DST 时区（不依赖 tzdata）：

    - 每年 3 月第 2 个周日 02:00 标准时间春跳夏令时（UTC-5 -> UTC-4，当天 23 小时）
    - 每年 11 月第 1 个周日 02:00 夏令时回拨标准时间（UTC-4 -> UTC-5，当天 25 小时）
    """

    STD_OFFSET = dt.timedelta(hours=-5)
    DST_OFFSET = dt.timedelta(hours=-4)

    @staticmethod
    def _nth_sunday(year: int, month: int, n: int) -> dt.date:
        d = dt.date(year, month, 1)
        offset = (6 - d.weekday()) % 7
        return d + dt.timedelta(days=offset + 7 * (n - 1))

    def _dst_bounds_utc(self, year: int) -> tuple[dt.datetime, dt.datetime]:
        spring_date = self._nth_sunday(year, 3, 2)
        fall_date = self._nth_sunday(year, 11, 1)
        # 02:00 EST -> 07:00 UTC
        spring_utc = dt.datetime(spring_date.year, spring_date.month, spring_date.day, 7, 0, tzinfo=dt.timezone.utc)
        # 02:00 EDT -> 06:00 UTC
        fall_utc = dt.datetime(fall_date.year, fall_date.month, fall_date.day, 6, 0, tzinfo=dt.timezone.utc)
        return spring_utc, fall_utc

    def fromutc(self, d: dt.datetime) -> dt.datetime:
        utc_naive = d.replace(tzinfo=None)
        utc_aware = utc_naive.replace(tzinfo=dt.timezone.utc)
        spring_utc, fall_utc = self._dst_bounds_utc(utc_aware.year)
        if spring_utc <= utc_aware < fall_utc:
            return (utc_naive + self.DST_OFFSET).replace(tzinfo=self)
        return (utc_naive + self.STD_OFFSET).replace(tzinfo=self)

    def utcoffset(self, d: dt.datetime | None) -> dt.timedelta:
        return self.STD_OFFSET + self.dst(d)

    def dst(self, d: dt.datetime | None) -> dt.timedelta:
        if d is None:
            return dt.timedelta(0)
        spring_date = self._nth_sunday(d.year, 3, 2)
        fall_date = self._nth_sunday(d.year, 11, 1)
        naive = d.replace(tzinfo=None)
        spring_start = dt.datetime(spring_date.year, spring_date.month, spring_date.day, 2, 0)
        fall_end = dt.datetime(fall_date.year, fall_date.month, fall_date.day, 2, 0)
        if spring_start <= naive < fall_end:
            if fall_end - dt.timedelta(hours=1) <= naive < fall_end and d.fold == 1:
                return dt.timedelta(0)
            return dt.timedelta(hours=1)
        return dt.timedelta(0)

    def tzname(self, d: dt.datetime | None) -> str:
        return "SYN_EDT" if self.dst(d) else "SYN_EST"


def _make_store(
    tmp_path: Path,
    *,
    initial_utc: dt.datetime | None = None,
    tz: dt.tzinfo | None = dt.timezone.utc,
    config: ProactiveConfig | None = None,
) -> tuple[ProactiveStore, ManualClock]:
    clock = ManualClock(
        initial_utc or dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=dt.timezone.utc),
        tz=tz,
    )
    store = ProactiveStore(tmp_path / "proactive.db", config=config, clock=clock)
    return store, clock


def test_default_disabled_and_db_under_state_dir_outside_vault(isolated_env: AppConfig) -> None:
    assert isolated_env.memory.proactive.enabled is False
    store = ProactiveStore.from_app_config(isolated_env)
    db_path = store.db_path.resolve()
    assert db_path.exists()
    assert isolated_env.broker.state_dir.resolve() in db_path.parents
    assert isolated_env.broker.vault_root.resolve() not in db_path.parents


def test_incremental_4_5_10_rounds_and_dedupe_and_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "lily.db"
    clock = ManualClock(dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    store = ProactiveStore(db_path, clock=clock)

    # 1..4 轮：不创建增量任务
    for i in range(1, 5):
        res = store.ingest_event(
            client="hermes",
            session_id="sess-1",
            event_id=f"turn-{i}",
            event_kind=EVENT_KIND_HUMAN_TURN,
            content=f"secret human message {i}",
        )
        assert res.accepted is True
        assert res.deduped is False
        assert res.turn_seq == i
        assert res.completed_human_turns == i
        assert res.created_job_id is None

    # 重复采集 turn-4：事务去重，计数不变
    dup = store.ingest_event(
        client="hermes",
        session_id="sess-1",
        event_id="turn-4",
        event_kind=EVENT_KIND_HUMAN_TURN,
        content="secret human message 4",
    )
    assert dup.deduped is True
    assert dup.completed_human_turns == 4
    assert dup.created_job_id is None

    # 模拟进程重启（重新实例化 ProactiveStore）
    store_restarted = ProactiveStore(db_path, clock=clock)
    res5 = store_restarted.ingest_event(
        client="hermes",
        session_id="sess-1",
        event_id="turn-5",
        event_kind=EVENT_KIND_HUMAN_TURN,
        content="secret human message 5",
    )
    assert res5.completed_human_turns == 5
    assert res5.created_job_id is not None

    # 6..9 不触发，第 10 轮触发 6..10
    for i in range(6, 10):
        r = store_restarted.ingest_event(
            client="hermes",
            session_id="sess-1",
            event_id=f"turn-{i}",
            event_kind=EVENT_KIND_HUMAN_TURN,
            content=f"msg {i}",
        )
        assert r.created_job_id is None

    res10 = store_restarted.ingest_event(
        client="hermes",
        session_id="sess-1",
        event_id="turn-10",
        event_kind=EVENT_KIND_HUMAN_TURN,
        content="msg 10",
    )
    assert res10.completed_human_turns == 10
    assert res10.created_job_id is not None

    # 状态输出不得包含任何聊天原文
    status = store_restarted.get_status()
    assert "secret human message" not in str(status)
    assert len(status["jobs"]) == 2
    assert status["jobs"][0]["batch_start"] == 1
    assert status["jobs"][0]["batch_end"] == 5
    assert status["jobs"][1]["batch_start"] == 6
    assert status["jobs"][1]["batch_end"] == 10


def test_nonhuman_cli_delegation_excluded_and_reference_only_evidence(tmp_path: Path) -> None:
    store, clock = _make_store(tmp_path)

    # system / heartbeat / review_generated 全部排除，不计数也不入库
    for kind in ("system", "heartbeat", "review_generated"):
        r = store.ingest_event(
            client="hermes",
            session_id="sess-a",
            event_id=f"ex-{kind}",
            event_kind=kind,
            content=f"ignored {kind}",
        )
        assert r.accepted is False
        assert r.excluded is True
        assert r.completed_human_turns == 0

    # reference_only（如 CLI 引用或委派结果）：不计入人类轮次，但作为会话证据保留
    ref_res = store.ingest_event(
        client="hermes",
        session_id="sess-a",
        event_id="cli-ref-1",
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content="CLI output ref",
    )
    assert ref_res.accepted is True
    assert ref_res.turn_seq is None
    assert ref_res.completed_human_turns == 0

    # 再来 5 轮真实人类轮次才触发 1..5 增量任务
    for i in range(1, 6):
        clock.advance(seconds=1)
        r = store.ingest_event(
            client="hermes",
            session_id="sess-a",
            event_id=f"h-{i}",
            event_kind=EVENT_KIND_HUMAN_TURN,
            content=f"human {i}",
        )
        assert r.completed_human_turns == i

    job = store.claim_job("worker-1")
    assert job is not None
    events = store.get_job_events(job.job_id)
    # 5 条 human_turn + 1 条 reference_only 均作为证据提供
    assert len(events) == 6
    assert any(e.event_kind == EVENT_KIND_REFERENCE_ONLY and e.content == "CLI output ref" for e in events)


def test_different_sessions_and_strict_batch_ordering(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)

    # sess-1 产生 10 轮（两个批次：1..5 与 6..10），sess-2 产生 5 轮（批次 1..5）
    for i in range(1, 11):
        store.ingest_event(
            client="hermes",
            session_id="sess-1",
            event_id=f"s1-{i}",
            content=f"s1 {i}",
        )
    for i in range(1, 6):
        store.ingest_event(
            client="agy",
            session_id="sess-2",
            event_id=f"s2-{i}",
            content=f"s2 {i}",
        )

    # 认领第 1 个任务：sess-1 (1..5)
    j1 = store.claim_job("w1")
    assert j1 is not None
    assert (j1.client, j1.session_id, j1.batch_start, j1.batch_end) == ("hermes", "sess-1", 1, 5)

    # 此时 sess-1 (6..10) 绝不能被认领，第二个认领者应拿到 sess-2 (1..5)
    j2 = store.claim_job("w2")
    assert j2 is not None
    assert (j2.client, j2.session_id, j2.batch_start, j2.batch_end) == ("agy", "sess-2", 1, 5)

    # 在 j1 完成前，没有更多可认领任务（sess-1 6..10 被阻塞等待 1..5 成功）
    assert store.claim_job("w3") is None

    # 即使 j1 失败进入 retry_wait，sess-1 (6..10) 仍被阻塞
    store.ack_job_failure(j1.job_id, j1.lease_token, error="transient", retry_delay_seconds=0)
    assert store.claim_job("w3").job_id == j1.job_id  # 重新认领 j1 而非 j3

    # j1 以 no_change 成功后，sess-1 (6..10) 才可认领
    j1_retry = store.claim_job("w3")
    # 上面已经 claim 了 j1，先拿到那个引用
    assert j1_retry is None  # j1 当前正在 running
    # 换个方式：测试单独函数验证 no_change 与进度


def test_no_change_ack_and_failure_retry_and_dead_letter(tmp_path: Path) -> None:
    cfg = ProactiveConfig(max_attempts=2, base_backoff_seconds=10)
    store, clock = _make_store(tmp_path, config=cfg)

    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"c-{i}")

    j = store.claim_job("w1")
    assert j is not None
    assert j.attempts == 1

    # 第一次失败：进入 retry_wait，last_reviewed_turn 仍为 0，completed_human_turns 为 5
    new_status = store.ack_job_failure(j.job_id, j.lease_token, error="timeout")
    assert new_status == JOB_STATUS_RETRY_WAIT
    st = store.get_status()
    assert st["sessions"][0]["completed_human_turns"] == 5
    assert st["sessions"][0]["last_reviewed_turn"] == 0
    assert st["jobs"][0]["last_error"] == "timeout"
    assert st["jobs"][0]["next_retry_at_utc"] is not None

    # 退避时间未到，不能认领
    assert store.claim_job("w1") is None

    # 推进时钟越过退避期，第二次认领并成功（no_change）
    clock.advance(seconds=11)
    j2 = store.claim_job("w1")
    assert j2 is not None
    assert j2.attempts == 2
    store.ack_job_success(j2.job_id, j2.lease_token, outcome="no_change")

    st2 = store.get_status()
    assert st2["sessions"][0]["last_reviewed_turn"] == 5
    assert st2["jobs"][0]["status"] == JOB_STATUS_SUCCEEDED
    assert st2["jobs"][0]["outcome"] == "no_change"

    # 再测试耗尽重试进入 dead
    for i in range(6, 11):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"c-{i}")
    jb = store.claim_job("w1")
    assert jb is not None
    assert store.ack_job_failure(jb.job_id, jb.lease_token, error="err1", retry_delay_seconds=0) == JOB_STATUS_RETRY_WAIT
    jb2 = store.claim_job("w1")
    assert jb2 is not None
    assert store.ack_job_failure(jb2.job_id, jb2.lease_token, error="err2") == JOB_STATUS_DEAD
    assert store.get_status()["sessions"][0]["last_reviewed_turn"] == 5


def test_abandoned_claim_reclaim_and_stale_lease_rejected(tmp_path: Path) -> None:
    store, clock = _make_store(tmp_path, config=ProactiveConfig(lease_seconds=30))
    for i in range(1, 6):
        store.ingest_event(client="hermes", session_id="s1", event_id=f"e-{i}", content=f"c-{i}")

    j_a = store.claim_job("worker-A")
    assert j_a is not None

    # 租约未过期，worker-B 无法抢占
    assert store.claim_job("worker-B") is None

    # 推进时钟使租约过期，worker-B 回收并重新认领
    clock.advance(seconds=31)
    j_b = store.claim_job("worker-B")
    assert j_b is not None
    assert j_b.job_id == j_a.job_id
    assert j_b.lease_token != j_a.lease_token
    assert j_b.attempts == 2

    # 过期的 worker-A 尝试 ack 必须被拒绝
    with pytest.raises(LeaseMismatchError):
        store.ack_job_success(j_a.job_id, j_a.lease_token, outcome="applied")

    # worker-B 正常 ack
    store.ack_job_success(j_b.job_id, j_b.lease_token, outcome="applied")
    assert store.get_status()["sessions"][0]["last_reviewed_turn"] == 5


def test_two_concurrent_claimers_no_double_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    clock = ManualClock(dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc), tz=dt.timezone.utc)
    store1 = ProactiveStore(db_path, clock=clock)
    store2 = ProactiveStore(db_path, clock=clock)

    for sess in ("s1", "s2"):
        for i in range(1, 6):
            store1.ingest_event(client="hermes", session_id=sess, event_id=f"{sess}-{i}", content="x")

    barrier = threading.Barrier(2)
    claimed_ids: list[int] = []
    lock = threading.Lock()

    def worker(st: ProactiveStore, wid: str) -> None:
        barrier.wait()
        job = st.claim_job(wid)
        if job is not None:
            with lock:
                claimed_ids.append(job.job_id)

    t1 = threading.Thread(target=worker, args=(store1, "w1"))
    t2 = threading.Thread(target=worker, args=(store2, "w2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(claimed_ids) == 2
    assert len(set(claimed_ids)) == 2


def test_nightly_23_boundary_missed_nights_empty_skipped_and_includes_all(tmp_path: Path) -> None:
    # 本地时区 UTC+8（仅测试注入任意固定偏移，非硬编码）
    tz_plus8 = dt.timezone(dt.timedelta(hours=8))
    # 激活时间：2026-11-17 13:00+08:00（基线 last_closed = 2026-11-16）
    init_utc = dt.datetime(2026, 11, 17, 13, 0, 0, tzinfo=tz_plus8).astimezone(dt.timezone.utc)
    store, clock = _make_store(tmp_path, initial_utc=init_utc, tz=tz_plus8)

    # 激活前不会伪造历史积压
    t0 = store.tick()
    assert t0.windows_checked == 0

    # 在 2026-11-17 窗口内写入：
    # - hermes 5 轮（已触发增量）+ 2 轮零头（共 7 轮）
    # - agy 1 轮
    # - reference_only 1 条
    for i in range(1, 8):
        store.ingest_event(
            client="hermes",
            session_id="h1",
            event_id=f"h-{i}",
            content=f"hermes {i}",
            occurred_at=dt.datetime(2026, 11, 17, 20, i, 0, tzinfo=tz_plus8),
        )
    store.ingest_event(
        client="agy",
        session_id="a1",
        event_id="agy-1",
        content="agy turn 1",
        occurred_at=dt.datetime(2026, 11, 17, 22, 59, 59, tzinfo=tz_plus8),  # 边界内最后一秒
    )
    # 23:00:00 整属于下一夜 2026-11-18
    store.ingest_event(
        client="hermes",
        session_id="h1",
        event_id="h-next-night",
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content="next night ref",
        occurred_at=dt.datetime(2026, 11, 17, 23, 0, 0, tzinfo=tz_plus8),
    )

    # 22:59:59 时 2026-11-17 尚未关闭
    clock.set_utc(dt.datetime(2026, 11, 17, 22, 59, 59, tzinfo=tz_plus8).astimezone(dt.timezone.utc))
    assert store.tick().windows_checked == 0

    # 跳过 3 个夜晚到 2026-11-20 01:00+08:00：
    # 应按时间顺序追平 2026-11-17（有 8 条）、2026-11-18（有 1 条 reference_only）、2026-11-19（空，跳过！）
    clock.set_utc(dt.datetime(2026, 11, 20, 1, 0, 0, tzinfo=tz_plus8).astimezone(dt.timezone.utc))
    res = store.tick()
    assert res.windows_checked == 3
    assert res.windows_skipped_empty == 1
    assert res.jobs_created == 2

    st = store.get_status()
    win_map = {w["window_id"]: w for w in st["nightly_windows"]}
    assert win_map["2026-11-17"]["status"] == "scheduled"
    assert win_map["2026-11-18"]["status"] == "scheduled"
    assert win_map["2026-11-19"]["status"] == "skipped_empty"

    nightly_jobs = [j for j in st["jobs"] if j["job_type"] == "nightly"]
    assert [j["window_id"] for j in nightly_jobs] == ["2026-11-17", "2026-11-18"]
    # 2026-11-17 包含已进入增量的 5 轮 + 零头 2 轮 + agy 1 轮 = 8 条
    ev_17 = store.get_job_events(nightly_jobs[0]["id"])
    assert len(ev_17) == 8
    assert {e.client for e in ev_17} == {"hermes", "agy"}
    # 2026-11-18 仅有一条 reference_only，也合格触发夜间任务
    ev_18 = store.get_job_events(nightly_jobs[1]["id"])
    assert len(ev_18) == 1
    assert ev_18[0].event_kind == EVENT_KIND_REFERENCE_ONLY


def test_late_imported_event_reopens_empty_and_completed_windows(tmp_path: Path) -> None:
    tz = dt.timezone.utc
    store, clock = _make_store(
        tmp_path,
        initial_utc=dt.datetime(2026, 11, 17, 10, 0, tzinfo=tz),
        tz=tz,
    )

    # 2026-11-17 有 1 条，2026-11-18 为空
    store.ingest_event(
        client="hermes",
        session_id="s1",
        event_id="e1",
        content="day 17",
        occurred_at=dt.datetime(2026, 11, 17, 15, 0, tzinfo=tz),
    )
    clock.set_utc(dt.datetime(2026, 11, 19, 1, 0, tzinfo=tz))
    store.tick()

    # 完成 2026-11-17 的 rev:1
    j17 = store.claim_job("w1")
    assert j17 is not None and j17.window_id == "2026-11-17" and j17.revision == 1
    store.ack_job_success(j17.job_id, j17.lease_token, outcome="applied")

    # 迟到导入：1 条落入已完成窗口 2026-11-17，1 条落入已跳过空窗口 2026-11-18
    store.ingest_event(
        client="agy",
        session_id="late-1",
        event_id="late-17",
        content="late for 17",
        occurred_at=dt.datetime(2026, 11, 17, 20, 0, tzinfo=tz),
    )
    store.ingest_event(
        client="agy",
        session_id="late-2",
        event_id="late-18",
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content="late for 18",
        occurred_at=dt.datetime(2026, 11, 18, 12, 0, tzinfo=tz),
    )

    t2 = store.tick()
    assert t2.windows_reopened == 2
    assert t2.jobs_created == 2

    j17_rev2 = store.claim_job("w1")
    assert j17_rev2 is not None and j17_rev2.window_id == "2026-11-17" and j17_rev2.revision == 2
    assert store.claim_job("w1") is None
    store.ack_job_success(j17_rev2.job_id, j17_rev2.lease_token, outcome="applied")

    j18_rev1 = store.claim_job("w1")
    assert j18_rev1 is not None and j18_rev1.window_id == "2026-11-18" and j18_rev1.revision == 1


def test_dst_spring_fall_windows_and_clock_rollback_no_duplicate(tmp_path: Path) -> None:
    dst_tz = SyntheticDSTZone()
    # 2026-11-01 为回拨日（25 小时）；先在 2026-10-31 23:30 初始化（baseline = 2026-10-31）
    init_utc = dt.datetime(2026, 10, 31, 23, 30, tzinfo=dst_tz).astimezone(dt.timezone.utc)
    store, clock = _make_store(tmp_path, initial_utc=init_utc, tz=dst_tz)

    store.ingest_event(
        client="hermes",
        session_id="dst",
        event_id="dst-1",
        content="during fall-back night",
        occurred_at=dt.datetime(2026, 11, 1, 1, 30, tzinfo=dst_tz, fold=1),
    )
    # 推进到 2026-11-02 00:00（2026-11-01 窗口已关闭）
    clock.set_utc(dt.datetime(2026, 11, 2, 0, 0, tzinfo=dst_tz).astimezone(dt.timezone.utc))
    r1 = store.tick()
    assert r1.windows_checked == 1
    assert r1.jobs_created == 1

    win = store.get_status()["nightly_windows"][0]
    assert win["window_id"] == "2026-11-01"
    # 回拨夜：前日 23:00 EDT (-14400s) 到 当日 23:00 EST (-18000s)，跨度 25 小时
    assert win["offset_start_seconds"] == -14400
    assert win["offset_end_seconds"] == -18000
    start_dt = dt.datetime.fromisoformat(win["window_start_utc"])
    end_dt = dt.datetime.fromisoformat(win["window_end_utc"])
    assert (end_dt - start_dt) == dt.timedelta(hours=25)

    # 时钟回拨到 2026-11-01 22:00 再前进回来，绝不产生重复窗口或重复任务
    clock.set_utc(dt.datetime(2026, 11, 1, 22, 0, tzinfo=dst_tz).astimezone(dt.timezone.utc))
    assert store.tick().jobs_created == 0
    clock.set_utc(dt.datetime(2026, 11, 2, 1, 0, tzinfo=dst_tz).astimezone(dt.timezone.utc))
    assert store.tick().jobs_created == 0
    assert len([j for j in store.get_status()["jobs"] if j["job_type"] == "nightly"]) == 1

    # 另建独立库验证春跳日（2027-03-14，23 小时）
    spring_dir = tmp_path / "spring"
    spring_dir.mkdir()
    s_init = dt.datetime(2027, 3, 13, 23, 30, tzinfo=dst_tz).astimezone(dt.timezone.utc)
    s_store, s_clock = _make_store(spring_dir, initial_utc=s_init, tz=dst_tz)
    s_store.ingest_event(
        client="hermes",
        session_id="dst-spring",
        event_id="sp-1",
        content="during spring-forward night",
        occurred_at=dt.datetime(2027, 3, 14, 12, 0, tzinfo=dst_tz),
    )
    s_clock.set_utc(dt.datetime(2027, 3, 15, 0, 0, tzinfo=dst_tz).astimezone(dt.timezone.utc))
    sr = s_store.tick()
    assert sr.windows_checked == 1
    assert sr.jobs_created == 1
    swin = s_store.get_status()["nightly_windows"][0]
    assert swin["offset_start_seconds"] == -18000
    assert swin["offset_end_seconds"] == -14400
    assert (
        dt.datetime.fromisoformat(swin["window_end_utc"])
        - dt.datetime.fromisoformat(swin["window_start_utc"])
    ) == dt.timedelta(hours=23)


def test_bounded_payload_and_safe_retention_prune(tmp_path: Path) -> None:
    cfg = ProactiveConfig(max_payload_chars=256, retention_days=7)
    store, clock = _make_store(
        tmp_path,
        initial_utc=dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        tz=dt.timezone.utc,
        config=cfg,
    )

    # 写入超长负载与 5 轮事件（在 2026-11-01 窗口内）
    for i in range(1, 6):
        store.ingest_event(
            client="hermes",
            session_id="ret",
            event_id=f"r-{i}",
            content="A" * 1000,
            occurred_at=dt.datetime(2026, 11, 1, 12, i, tzinfo=dt.timezone.utc),
        )

    # 关闭 2026-11-01 窗口并推进 30 天
    clock.set_utc(dt.datetime(2026, 12, 1, 0, 0, tzinfo=dt.timezone.utc))
    store.tick()

    # 任务尚未完成前，prune_events 绝不删除待处理证据
    assert store.prune_events() == 0

    # 完成增量与夜间任务后，超期已消费事件可被安全清理；长正文通过无损分块还原
    while (job := store.claim_job("w1")) is not None:
        events = store.get_job_events(job.job_id)
        assert all(len(e.content) == 1000 and not e.content_truncated for e in events)
        store.ack_job_success(job.job_id, job.lease_token, outcome="no_change")

    deleted = store.prune_events()
    assert deleted == 5


def test_oversize_payload_rejected_not_truncated(tmp_path: Path) -> None:
    cfg = ProactiveConfig(max_payload_chars=256, max_payload_chunks=2)
    store, _ = _make_store(
        tmp_path,
        initial_utc=dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        tz=dt.timezone.utc,
        config=cfg,
    )
    with pytest.raises(PayloadTooLargeError):
        store.ingest_event(
            client="hermes",
            session_id="s-big",
            event_id="big-1",
            content="X" * 600,
        )
    status = store.get_status()
    assert status["sessions"] == []
    assert status["jobs"] == []


def test_nightly_catchup_chronological_across_windows(tmp_path: Path) -> None:
    store, clock = _make_store(
        tmp_path,
        initial_utc=dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc),
        tz=dt.timezone.utc,
    )
    store.ingest_event(
        client="hermes",
        session_id="s-chrono",
        event_id="c-1",
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content="day 1",
        occurred_at=dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    store.ingest_event(
        client="hermes",
        session_id="s-chrono",
        event_id="c-2",
        event_kind=EVENT_KIND_REFERENCE_ONLY,
        content="day 2",
        occurred_at=dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc),
    )
    clock.set_utc(dt.datetime(2026, 11, 3, 0, 0, tzinfo=dt.timezone.utc))
    tick = store.tick()
    assert tick.jobs_created == 2

    j1 = store.claim_job("w1")
    assert j1 is not None and j1.window_id == "2026-11-01"
    # 2026-11-01 失败进入 retry_wait 时，绝不能跳过它去认领 2026-11-02
    store.ack_job_failure(j1.job_id, j1.lease_token, error="temp", retry_delay_seconds=60)
    assert store.claim_job("w1") is None

    clock.advance(seconds=61)
    j1_retry = store.claim_job("w1")
    assert j1_retry is not None and j1_retry.window_id == "2026-11-01"
    store.ack_job_success(j1_retry.job_id, j1_retry.lease_token, outcome="no_change")

    j2 = store.claim_job("w1")
    assert j2 is not None and j2.window_id == "2026-11-02"
    store.ack_job_success(j2.job_id, j2.lease_token, outcome="no_change")
