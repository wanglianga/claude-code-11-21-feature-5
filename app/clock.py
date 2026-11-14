"""可推进虚拟时钟：系统内所有"现在"的统一来源，保证演示与测试可复现。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Clock

CLOCK_ROW_ID = 1


def ensure_clock(session: Session) -> None:
    if session.get(Clock, CLOCK_ROW_ID) is None:
        session.add(Clock(id=CLOCK_ROW_ID, now=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        session.commit()


def now(session: Session) -> datetime:
    row = session.get(Clock, CLOCK_ROW_ID)
    if row is None:
        ensure_clock(session)
        row = session.get(Clock, CLOCK_ROW_ID)
    assert row is not None
    return row.now


def advance(session: Session, *, seconds: float | None = None, to: datetime | None = None) -> datetime:
    """推进时钟；只允许向前，拒绝回拨。"""
    current = now(session)
    if to is not None:
        target = to
    elif seconds is not None:
        if seconds < 0:
            raise ValueError("seconds 必须为非负数")
        target = current + timedelta(seconds=seconds)
    else:
        raise ValueError("必须提供 seconds 或 to")
    if target < current:
        raise ValueError(f"不允许回拨时钟：当前 {current.isoformat()}，目标 {target.isoformat()}")
    row = session.get(Clock, CLOCK_ROW_ID)
    row.now = target
    session.commit()
    return target
