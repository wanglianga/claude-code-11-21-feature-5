"""pytest 基础设施：独立测试库、每用例清表、统一构造助手。"""
from __future__ import annotations

import os
import uuid

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev_test"
)

import psycopg
import pytest
from sqlalchemy import text

MAINTENANCE_DSN = os.environ.get(
    "TEST_MAINTENANCE_DSN", "host=127.0.0.1 port=5432 user=ccdev password=ccdev dbname=ccdev"
)


def _ensure_test_db() -> None:
    dbname = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].split("?")[0]
    with psycopg.connect(MAINTENANCE_DSN, autocommit=True) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    _ensure_test_db()
    from app import models  # noqa: F401  确保所有表注册到 Base.metadata
    from app.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    from app.db import Base, get_engine, get_session_factory
    from app import clock as clock_mod

    engine = get_engine()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
    with get_session_factory()() as s:
        clock_mod.ensure_clock(s)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- 助手

def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_experiment(client, *, name=None, seed="seed-1", obs=7, ratios=None,
                    rule=None, chi2_threshold=0.001, enrollment_definition=None):
    body = {
        "name": name or uniq("exp"),
        "seed": seed,
        "observation_days": obs,
        "ratios": ratios or {"control": 0.5, "treatment": 0.5},
        "chi2_threshold": chi2_threshold,
    }
    if rule is not None:
        body["audience_rule"] = rule
    if enrollment_definition is not None:
        body["enrollment_definition"] = enrollment_definition
    r = client.post("/experiments", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def assign(client, eid, req, key=None, **kw):
    body = {"request_id": req}
    if key is not None:
        body["student_key"] = key
    body.update(kw)
    r = client.post(f"/experiments/{eid}/assignments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def advance(client, seconds):
    r = client.post("/clock/advance", json={"seconds": seconds})
    assert r.status_code == 200, r.text
    return r.json()["now"]


def ingest(client, events, watermarks=None):
    r = client.post("/events", json={"events": events, "watermarks": watermarks or []})
    assert r.status_code == 200, r.text
    return r.json()


def run_analysis(client, eid):
    r = client.post(f"/experiments/{eid}/analysis")
    assert r.status_code == 201, r.text
    return r.json()


def order_event(key, biz, seq, occurred_at, source="orders", event_type="order_created"):
    return {
        "source": source,
        "biz_id": biz,
        "seq": seq,
        "event_type": event_type,
        "student_key": key,
        "occurred_at": occurred_at,
    }


DAY = 86400
