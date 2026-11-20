"""Stage 2 后台主动评审运行器（单写者耐久租约、启动/周期 tick、干净退出与导出）。"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from agentbrain.config import AppConfig
from agentbrain.proactive import ProactiveStore
from agentbrain.proactive_adapters import (
    HermesStateDbScanReport,
    IdeScanReport,
    resolve_hermes_statedb_path,
    scan_agy_ide_roots,
    scan_hermes_statedb,
)
from agentbrain.proactive_clock import Clock, SystemClock, ensure_utc
from agentbrain.proactive_redaction import redact_secrets
from agentbrain.proactive_worker import run_worker_once, segment_job_prompts


class ProactiveRunner:
    """受控后台主动评审循环：enabled=False 时绝不启动线程或发起任何网络请求。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        write_lock: threading.Lock | None = None,
        clock: Clock | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.write_lock = write_lock or threading.Lock()
        self.owner_id = owner_id or f"pid-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease_token: str | None = None
        self._last_ide_scan_utc: dt.datetime | None = None
        self.last_ide_report: IdeScanReport | None = None
        self.last_hermes_report: HermesStateDbScanReport | None = None
        self.last_error: str | None = None

    def _make_store(self) -> ProactiveStore:
        return ProactiveStore.from_app_config(self.config, clock=self.clock)

    def _log_cycle(self, payload: dict[str, Any]) -> None:
        """把后台周期结果追加到运行日志（只含计数与错误，不含对话正文）。"""
        try:
            log_dir = self.config.broker.log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            record = {"ts": ensure_utc(self.clock.now_utc()).isoformat(), **payload}
            with (log_dir / "proactive-runner.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _persist_runner_status(self, store: ProactiveStore) -> None:
        try:
            hermes_diag: dict[str, Any] | None = None
            if self.last_hermes_report is not None:
                hr = self.last_hermes_report
                hermes_diag = {
                    "available": hr.available,
                    "sessions_scanned": hr.sessions_scanned,
                    "turns_completed": hr.turns_completed,
                    "events_ingested": hr.events_ingested,
                    "events_deduped": hr.events_deduped,
                    "skipped_pre_activation": hr.skipped_pre_activation,
                    "skipped_excluded": hr.skipped_excluded,
                    "last_error": hr.last_error,
                }
            store.set_adapter_cursor(
                "proactive_runner",
                "status",
                {
                    "owner_id": self.owner_id,
                    "last_error": redact_secrets(self.last_error)[:300] if self.last_error else None,
                    "unavailable_sources": (
                        list(self.last_ide_report.unavailable_sources)
                        if self.last_ide_report is not None
                        else []
                    ),
                    "hermes_statedb": hermes_diag,
                    "updated_at_utc": ensure_utc(self.clock.now_utc()).isoformat(),
                },
            )
        except Exception:
            pass

    def _renew_or_acquire_lease(self, store: ProactiveStore, lease_sec: int) -> bool:
        if self._lease_token is None:
            self._lease_token = store.try_acquire_runner_lease(
                self.owner_id,
                lease_seconds=lease_sec,
            )
        else:
            if not store.renew_runner_lease(
                self.owner_id,
                self._lease_token,
                lease_seconds=lease_sec,
            ):
                self._lease_token = store.try_acquire_runner_lease(
                    self.owner_id,
                    lease_seconds=lease_sec,
                )
        return self._lease_token is not None

    def run_cycle(self, *, max_jobs: int = 10) -> dict[str, Any]:
        """执行一次完整的恢复 + 扫描 + tick + 认领处理 + 清理周期。"""
        if not self.config.memory.proactive.enabled:
            return {"enabled": False, "skipped": True}

        store = self._make_store()
        lease_sec = max(30, self.config.memory.proactive.lease_seconds)
        if not self._renew_or_acquire_lease(store, lease_sec):
            return {"enabled": True, "lease_acquired": False}

        store.recover_expired_leases()

        ide_report: IdeScanReport | None = None
        if self.config.memory.proactive.agy_ide_enabled and self.config.memory.proactive.agy_ide_roots:
            now_utc = ensure_utc(self.clock.now_utc())
            ide_poll = max(1, int(self.config.memory.proactive.agy_ide_poll_seconds))
            if (
                self._last_ide_scan_utc is None
                or (now_utc - self._last_ide_scan_utc).total_seconds() >= ide_poll
            ):
                ide_report = scan_agy_ide_roots(
                    store,
                    self.config.memory.proactive.agy_ide_roots,
                    catchup=False,
                )
                self._last_ide_scan_utc = now_utc
                self.last_ide_report = ide_report

        hermes_report: HermesStateDbScanReport | None = None
        hermes_db_path = resolve_hermes_statedb_path(self.config)
        if hermes_db_path is not None:
            hermes_report = scan_hermes_statedb(store, hermes_db_path, catchup=False)
            self.last_hermes_report = hermes_report

        tick_res = store.tick()

        def _heartbeat() -> bool:
            if self._lease_token is None:
                return False
            return store.renew_runner_lease(self.owner_id, self._lease_token, lease_seconds=lease_sec)

        processed = 0
        for _ in range(max_jobs):
            if self._stop_event.is_set():
                break
            if not _heartbeat():
                break
            res = run_worker_once(
                self.config,
                store,
                worker_id=self.owner_id,
                write_lock=self.write_lock,
                clock=self.clock,
                lease_heartbeat=_heartbeat,
            )
            if res is None:
                break
            processed += 1

        pruned = store.prune_events()
        self._persist_runner_status(store)
        return {
            "enabled": True,
            "lease_acquired": True,
            "tick": {
                "windows_checked": tick_res.windows_checked,
                "windows_skipped_empty": tick_res.windows_skipped_empty,
                "jobs_created": tick_res.jobs_created,
                "windows_reopened": tick_res.windows_reopened,
                "windows_requeued_dead": tick_res.windows_requeued_dead,
            },
            "ide_turns": ide_report.turns_completed if ide_report else 0,
            "hermes_turns": hermes_report.turns_completed if hermes_report else 0,
            "hermes_ingested": hermes_report.events_ingested if hermes_report else 0,
            "hermes_deduped": hermes_report.events_deduped if hermes_report else 0,
            "jobs_processed": processed,
            "events_pruned": pruned,
        }

    def start(self) -> bool:
        """若 proactive.enabled 为 True，启动后台线程；否则返回 False。"""
        if not self.config.memory.proactive.enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True

        self._stop_event.clear()

        def _loop() -> None:
            poll = max(1, self.config.memory.proactive.worker_poll_seconds)
            while not self._stop_event.is_set():
                try:
                    res = self.run_cycle()
                    self.last_error = None
                    self._log_cycle({"ok": True, "cycle": res})
                except Exception as exc:  # noqa: BLE001
                    self.last_error = redact_secrets(f"{type(exc).__name__}: {exc}")[:300]
                    self._log_cycle({"ok": False, "error": self.last_error})
                try:
                    self._persist_runner_status(self._make_store())
                except Exception as exc:  # noqa: BLE001
                    self._log_cycle({"ok": False, "persist_error": redact_secrets(f"{type(exc).__name__}: {exc}")[:300]})
                self._stop_event.wait(poll)

        self._thread = threading.Thread(
            target=_loop,
            name="agentbrain-proactive-runner",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止后台线程；仅在工作线程确实停止后才释放耐久租约。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # 线程仍在运行，绝不提前释放租约以防并发双写
                return
            self._thread = None
        if self._lease_token is not None:
            try:
                store = self._make_store()
                store.release_runner_lease(self.owner_id, self._lease_token)
            except Exception:
                pass
            self._lease_token = None


def export_review_prompts(
    config: AppConfig,
    store: ProactiveStore,
    out_dir: Path,
) -> list[Path]:
    """导出待处理任务的评审提示词到指定目录，不认领、不写 Vault、不调用模型。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    status = store.get_status()
    exported: list[Path] = []
    for job_info in status.get("jobs", []):
        if job_info.get("status") not in ("pending", "retry_wait"):
            continue
        job_id = int(job_info["id"])
        events = store.get_job_events(job_id)
        from agentbrain.proactive import ClaimedJob

        pseudo_job = ClaimedJob(
            job_id=job_id,
            idempotency_key=str(job_info["idempotency_key"]),
            job_type=str(job_info["job_type"]),
            attempts=int(job_info["attempts"]),
            max_attempts=int(job_info["max_attempts"]),
            lease_token="export-dry-run",
            lease_expires_at_utc="",
            client=job_info.get("client"),
            session_id=job_info.get("session_id"),
            batch_start=job_info.get("batch_start"),
            batch_end=job_info.get("batch_end"),
            window_id=job_info.get("window_id"),
            revision=job_info.get("revision"),
            since_event_id=None,
            until_event_id=None,
            event_count=int(job_info["event_count"]),
        )
        segments = segment_job_prompts(config, pseudo_job, events)
        target = out_dir / f"job_{job_id}_prompts.json"
        target.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "idempotency_key": pseudo_job.idempotency_key,
                    "segments": [
                        {"segment_index": idx, "event_ids": [e.event_id for e in evs], "prompt": p}
                        for idx, (evs, p) in enumerate(segments)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        exported.append(target)
    return exported
