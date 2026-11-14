"""接口杂项：时钟、健康检查、OpenAPI 文档、统计函数正确性。"""
from __future__ import annotations

import math

from app.stats import chi2_sf, diff_ci

from conftest import DAY, make_experiment


def test_health_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for p in ("/experiments", "/clock/advance", "/events", "/analysis/{run_id}"):
        assert p in paths
    # Swagger UI 可用
    assert client.get("/docs").status_code == 200


def test_clock_monotonic(client):
    r = client.get("/clock")
    assert r.json()["timezone"] == "UTC"
    client.post("/clock/advance", json={"seconds": DAY})
    r = client.post("/clock/advance", json={"seconds": -1})
    assert r.status_code == 422  # pydantic ge=0
    r = client.post("/clock/advance", json={"to": "2025-12-01T00:00:00Z"})
    assert r.status_code == 400  # 拒绝回拨


def test_chi2_sf_known_values():
    assert math.isclose(chi2_sf(3.841, 1), 0.05, abs_tol=1e-3)
    assert math.isclose(chi2_sf(6.635, 1), 0.01, abs_tol=1e-3)
    assert math.isclose(chi2_sf(10.828, 1), 0.001, abs_tol=1e-4)
    assert math.isclose(chi2_sf(5.991, 2), 0.05, abs_tol=1e-3)
    assert chi2_sf(0.0, 1) == 1.0


def test_diff_ci_known_value():
    # 两组各 100，p1=0.2, p2=0.1：se = sqrt(0.16/100 + 0.09/100) = 0.05
    diff, low, high = diff_ci(0.2, 100, 0.1, 100)
    assert math.isclose(diff, 0.1, abs_tol=1e-12)
    assert math.isclose(low, 0.1 - 1.96 * 0.05, abs_tol=1e-3)
    assert math.isclose(high, 0.1 + 1.96 * 0.05, abs_tol=1e-3)


def test_experiment_immutable_rule(client):
    """受众规则变化需新开实验：不提供规则更新接口。"""
    exp = make_experiment(client, rule={"conditions": [{"field": "age", "op": "gte", "value": 18}]})
    eid = exp["id"]
    r = client.put(f"/experiments/{eid}", json={"audience_rule": {}})
    assert r.status_code == 405  # 无此方法
    r = client.patch(f"/experiments/{eid}", json={"audience_rule": {}})
    assert r.status_code == 405
