"""时钟抽象与 OS 本地日历（DST 安全）夜间窗口计算模块。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

EPOCH_UTC = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """将 datetime 规范化为带 UTC 时区的 aware datetime。"""
    if value.tzinfo is None:
        return value.astimezone(dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def dt_to_us(value: dt.datetime) -> int:
    """将 datetime 精确转换为 UTC 微秒时间戳（整数运算，无浮点误差）。"""
    utc_dt = ensure_utc(value)
    return (utc_dt - EPOCH_UTC) // dt.timedelta(microseconds=1)


def us_to_dt(us: int) -> dt.datetime:
    """将 UTC 微秒时间戳还原为 UTC aware datetime。"""
    return EPOCH_UTC + dt.timedelta(microseconds=us)


def us_to_iso(us: int) -> str:
    """将 UTC 微秒时间戳格式化为 ISO-8601 UTC 字符串。"""
    return us_to_dt(us).isoformat()


class Clock(Protocol):
    """可注入时钟协议：生产环境用 SystemClock，测试可注入 ManualClock。"""

    def now_utc(self) -> dt.datetime:
        """返回当前 UTC aware datetime。"""
        ...

    def to_local(self, utc_dt: dt.datetime) -> dt.datetime:
        """将 UTC datetime 转换为本地壁钟时间。"""
        ...

    def local_wall_to_utc(self, local_naive: dt.datetime, fold: int = 0) -> dt.datetime:
        """将本地壁钟 naive datetime（按 fold）转换为 UTC aware datetime。"""
        ...


class SystemClock:
    """默认系统时钟：使用 OS 本地时区（不硬编码任何时区）。"""

    def __init__(self, tz: dt.tzinfo | None = None) -> None:
        self._tz = tz

    def now_utc(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def to_local(self, utc_dt: dt.datetime) -> dt.datetime:
        aware = ensure_utc(utc_dt)
        if self._tz is None:
            return aware.astimezone()
        return aware.astimezone(self._tz)

    def local_wall_to_utc(self, local_naive: dt.datetime, fold: int = 0) -> dt.datetime:
        if local_naive.tzinfo is not None:
            return local_naive.astimezone(dt.timezone.utc)
        if self._tz is None:
            return local_naive.replace(fold=fold).astimezone(dt.timezone.utc)
        return local_naive.replace(tzinfo=self._tz, fold=fold).astimezone(dt.timezone.utc)


class ManualClock:
    """确定性测试时钟，支持时间推进、回拨与自定义 DST 时区注入。"""

    def __init__(
        self,
        initial_utc: dt.datetime,
        tz: dt.tzinfo | None = None,
    ) -> None:
        self._now_utc = ensure_utc(initial_utc)
        self._tz = tz

    def now_utc(self) -> dt.datetime:
        return self._now_utc

    def set_utc(self, new_utc: dt.datetime) -> None:
        self._now_utc = ensure_utc(new_utc)

    def advance(
        self,
        *,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
        microseconds: int = 0,
    ) -> dt.datetime:
        self._now_utc = self._now_utc + dt.timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
            microseconds=microseconds,
        )
        return self._now_utc

    def to_local(self, utc_dt: dt.datetime) -> dt.datetime:
        aware = ensure_utc(utc_dt)
        if self._tz is None:
            return aware.astimezone()
        return aware.astimezone(self._tz)

    def local_wall_to_utc(self, local_naive: dt.datetime, fold: int = 0) -> dt.datetime:
        if local_naive.tzinfo is not None:
            return local_naive.astimezone(dt.timezone.utc)
        if self._tz is None:
            return local_naive.replace(fold=fold).astimezone(dt.timezone.utc)
        return local_naive.replace(tzinfo=self._tz, fold=fold).astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class NightlyWindowSpec:
    """单个本地日历夜间窗口 [prev_day 23:00, day 23:00) 的 UTC 边界与偏移快照。"""

    window_id: str
    window_date: dt.date
    start_utc: dt.datetime
    end_utc: dt.datetime
    start_us: int
    end_us: int
    offset_start_seconds: int
    offset_end_seconds: int
    tz_label: str


def compute_nightly_window(
    window_date: dt.date,
    clock: Clock,
    *,
    hour: int = 23,
    minute: int = 0,
) -> NightlyWindowSpec:
    """按 OS 本地壁钟计算 [前一天 23:00, 当天 23:00) 的 DST 安全 UTC 区间。"""
    prev_date = window_date - dt.timedelta(days=1)
    start_naive = dt.datetime(prev_date.year, prev_date.month, prev_date.day, hour, minute, 0)
    end_naive = dt.datetime(window_date.year, window_date.month, window_date.day, hour, minute, 0)

    start_utc = clock.local_wall_to_utc(start_naive, fold=0)
    end_utc = clock.local_wall_to_utc(end_naive, fold=0)

    start_local = clock.to_local(start_utc)
    end_local = clock.to_local(end_utc)

    offset_start = int((start_local.utcoffset() or dt.timedelta(0)).total_seconds())
    offset_end = int((end_local.utcoffset() or dt.timedelta(0)).total_seconds())
    tz_label = end_local.tzname() or "LOCAL"

    return NightlyWindowSpec(
        window_id=window_date.isoformat(),
        window_date=window_date,
        start_utc=start_utc,
        end_utc=end_utc,
        start_us=dt_to_us(start_utc),
        end_us=dt_to_us(end_utc),
        offset_start_seconds=offset_start,
        offset_end_seconds=offset_end,
        tz_label=tz_label,
    )


def latest_closed_window_date(
    now_utc: dt.datetime,
    clock: Clock,
    *,
    hour: int = 23,
    minute: int = 0,
) -> dt.date:
    """返回截至 now_utc 已关闭（now_utc >= 当日 23:00）的最新夜间窗口日期。"""
    now_local = clock.to_local(now_utc)
    d = now_local.date()
    cutoff_utc = clock.local_wall_to_utc(
        dt.datetime(d.year, d.month, d.day, hour, minute, 0),
        fold=0,
    )
    if ensure_utc(now_utc) >= cutoff_utc:
        return d
    return d - dt.timedelta(days=1)
