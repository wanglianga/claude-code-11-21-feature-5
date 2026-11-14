"""事件摄取：去重、水位线单调、有效水位取最小。"""
from __future__ import annotations

from sqlalchemy import select

from conftest import ingest, make_experiment  # noqa: F401  (复用 fixture 依赖链)


def test_dedup_by_source_biz_seq(client):
    e = {"source": "orders", "biz_id": "o-1", "seq": 1, "event_type": "order_created",
         "student_key": "stu-1", "occurred_at": "2026-01-02T00:00:00Z", "payload": {"amount": 100}}
    r1 = ingest(client, [e])
    assert r1["inserted"] == 1 and r1["duplicates"] == []

    # 同 (source, biz, seq) 再次上报（内容不同）→ 去重，首条内容不变
    e2 = dict(e, payload={"amount": 999})
    r2 = ingest(client, [e2])
    assert r2["inserted"] == 0
    assert r2["duplicates"] == [{"source": "orders", "biz_id": "o-1", "seq": 1}]

    from app.db import get_session_factory
    from app.models import Event
    with get_session_factory()() as s:
        rows = list(s.scalars(select(Event)))
        assert len(rows) == 1
        assert rows[0].payload["amount"] == 100  # 不用后来的内容重写过去事实

    # 同 biz 不同 seq（如取消事件）→ 独立事件
    e3 = dict(e, seq=2, event_type="order_canceled", occurred_at="2026-01-03T00:00:00Z")
    r3 = ingest(client, [e3])
    assert r3["inserted"] == 1


def test_watermark_monotonic(client):
    ingest(client, [], [{"source": "orders", "complete_until": "2026-01-10T00:00:00Z"}])
    r = client.post("/events", json={"events": [], "watermarks": [
        {"source": "orders", "complete_until": "2026-01-05T00:00:00Z"}]})
    assert r.status_code == 409  # 水位只允许前进


def test_effective_watermark_is_min(client):
    ingest(client, [], [
        {"source": "orders", "complete_until": "2026-01-10T00:00:00Z"},
        {"source": "app-events", "complete_until": "2026-01-08T00:00:00Z"},
    ])
    r = client.get("/sources")
    assert r.status_code == 200
    assert r.json()["effective_watermark"] == "2026-01-08T00:00:00+00:00"


def test_event_seq_monotonic(client):
    r1 = ingest(client, [{"source": "s", "biz_id": "b1", "seq": 1, "event_type": "t",
                          "student_key": "k", "occurred_at": "2026-01-01T00:00:00Z"}])
    r2 = ingest(client, [{"source": "s", "biz_id": "b2", "seq": 1, "event_type": "t",
                          "student_key": "k", "occurred_at": "2026-01-01T00:00:00Z"}])
    assert r2["max_event_seq"] > r1["max_event_seq"]


def test_standalone_watermark_endpoint(client):
    r = client.post("/sources/orders/watermark",
                    json={"source": "orders", "complete_until": "2026-01-10T00:00:00Z"})
    assert r.status_code == 200
    assert r.json()["effective_watermark"] == "2026-01-10T00:00:00+00:00"
    # 路径与请求体 source 不一致 → 400
    r = client.post("/sources/other/watermark",
                    json={"source": "orders", "complete_until": "2026-01-11T00:00:00Z"})
    assert r.status_code == 400
    # 回退水位 → 409
    r = client.post("/sources/orders/watermark",
                    json={"source": "orders", "complete_until": "2026-01-01T00:00:00Z"})
    assert r.status_code == 409
