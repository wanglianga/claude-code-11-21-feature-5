"""行为事件摄取：按 (source, biz_id, seq) 去重；维护各来源"已完整上报"水位线。

- 事件不可变：取消/退款是独立新事件（新 seq），不得用"今天的订单状态"重写过去事实。
- 撤销可能先于原单到达：摄取顺序不影响分析，分析只按 occurred_at 评估，
  原单未到齐时撤销自然"等待"（对未知订单无影响），原单到达后的新分析才会关联。
- 水位线只增不减：complete_until 表示该来源此前时刻的事件已完整上报。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import clock
from .models import Event, Source
from .services import DomainError


def effective_watermark(session: Session) -> datetime | None:
    """全局有效水位 = 所有来源 complete_until 的最小值；无来源时为 None。"""
    return session.scalar(select(func.min(Source.complete_until)))


def max_event_seq(session: Session) -> int:
    return session.scalar(select(func.coalesce(func.max(Event.id), 0))) or 0


def set_watermark(session: Session, source: str, complete_until: datetime) -> Source:
    now = clock.now(session)
    row = session.get(Source, source)
    if row is None:
        row = Source(name=source, complete_until=complete_until, updated_at=now)
        session.add(row)
    else:
        if complete_until < row.complete_until:
            raise DomainError(
                f"来源 {source} 的水位线只允许前进：当前 {row.complete_until.isoformat()}，"
                f"收到 {complete_until.isoformat()}",
                409,
            )
        row.complete_until = complete_until
        row.updated_at = now
    session.flush()
    return row


def ingest_events(
    session: Session,
    events: list[dict],
    watermarks: list[dict] | None = None,
) -> dict:
    """批量摄取。先校验并应用水位线，再逐条去重插入事件。"""
    now = clock.now(session)

    # 水位线先全部校验（避免部分应用）
    if watermarks:
        for w in watermarks:
            src, until = w["source"], w["complete_until"]
            existing = session.get(Source, src)
            if existing is not None and until < existing.complete_until:
                raise DomainError(
                    f"来源 {src} 的水位线只允许前进：当前 {existing.complete_until.isoformat()}，"
                    f"收到 {until.isoformat()}",
                    409,
                )
        for w in watermarks:
            set_watermark(session, w["source"], w["complete_until"])

    inserted, duplicates = 0, []
    for e in events:
        stmt = (
            pg_insert(Event)
            .values(
                source=e["source"],
                biz_id=e["biz_id"],
                seq=e["seq"],
                event_type=e["event_type"],
                student_key=e["student_key"],
                occurred_at=e["occurred_at"],
                payload=e.get("payload") or {},
                ingested_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_event_source_biz_seq")
            .returning(Event.id)
        )
        new_id = session.execute(stmt).scalar_one_or_none()
        if new_id is None:
            duplicates.append(
                {"source": e["source"], "biz_id": e["biz_id"], "seq": e["seq"]}
            )
        else:
            inserted += 1
        # 来源登记（不改变已有水位）
        if session.get(Source, e["source"]) is None:
            session.add(
                Source(name=e["source"], complete_until=e["occurred_at"], updated_at=now)
            )
            session.flush()
    session.commit()
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "effective_watermark": (
            wm.isoformat() if (wm := effective_watermark(session)) else None
        ),
        "max_event_seq": max_event_seq(session),
    }
