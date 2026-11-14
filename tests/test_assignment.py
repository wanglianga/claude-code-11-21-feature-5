"""分配行为：幂等、跨设备、资格快照、停招、比例调整、曝光保留。"""
from __future__ import annotations

from app.services import variant_for

from conftest import DAY, advance, assign, make_experiment, run_analysis, uniq


def test_create_experiment_validation(client):
    r = client.post("/experiments", json={
        "name": uniq("bad"), "seed": "s", "observation_days": 7,
        "ratios": {"control": 0.5},  # 缺少 treatment
    })
    assert r.status_code == 400

    r = client.post("/experiments", json={
        "name": uniq("bad"), "seed": "s", "observation_days": 7,
        "ratios": {"control": 0.6, "treatment": 0.6},  # 和不为 1
    })
    assert r.status_code == 400

    r = client.post("/experiments", json={
        "name": uniq("bad"), "seed": "s", "observation_days": 0,
        "ratios": {"control": 0.5, "treatment": 0.5},
    })
    assert r.status_code == 422  # pydantic ge=1

    exp = make_experiment(client)
    r = client.post("/experiments", json={
        "name": exp["name"], "seed": "s2", "observation_days": 7,
        "ratios": {"control": 0.5, "treatment": 0.5},
    })
    assert r.status_code == 409  # 名称唯一


def test_assign_deterministic_and_duplicates(client):
    exp = make_experiment(client, seed="fixed-seed")
    eid = exp["id"]

    r1 = assign(client, eid, "req-1", "stu-001")
    assert r1["outcome"] == "assigned"
    # 哈希可复现：与独立计算一致
    assert r1["variant"] == variant_for("fixed-seed", "stu-001", {"control": 0.5, "treatment": 0.5})

    # 同 request_id 重放 → 幂等返回首次结果
    r2 = assign(client, eid, "req-1", "stu-001")
    assert r2["outcome"] == "duplicate" and r2["idempotent_replay"] is True
    assert r2["variant"] == r1["variant"]

    # 同 request_id 但不同学员 → 冲突
    r = client.post(f"/experiments/{eid}/assignments",
                    json={"request_id": "req-1", "student_key": "stu-other"})
    assert r.status_code == 409

    # 同学员不同 request_id → 返回首次事实，组不变
    r3 = assign(client, eid, "req-2", "stu-001")
    assert r3["outcome"] == "duplicate" and r3["idempotent_replay"] is False
    assert r3["variant"] == r1["variant"]
    assert r3["assigned_at"] == r1["assigned_at"]


def test_cross_device_same_group(client):
    exp = make_experiment(client)
    eid = exp["id"]

    r1 = assign(client, eid, "req-1", "stu-100", device_id="dev-phone")
    assert r1["outcome"] == "assigned"

    # 换设备、同一学员标识 → 返回原组
    r2 = assign(client, eid, "req-2", "stu-100", device_id="dev-pad")
    assert r2["outcome"] == "duplicate" and r2["variant"] == r1["variant"]

    # 仅带旧设备（匿名请求）→ 解析到同一学员，仍是原组
    r3 = assign(client, eid, "req-3", device_id="dev-phone")
    assert r3["outcome"] == "duplicate"
    assert r3["student_key"] == "stu-100" and r3["variant"] == r1["variant"]

    # 全新设备匿名 → 分配新学员
    r4 = assign(client, eid, "req-4", device_id="dev-new")
    assert r4["outcome"] == "assigned" and r4["student_key"]


def test_ineligible_first_fact_kept(client):
    rule = {"conditions": [{"field": "age", "op": "gte", "value": 18}]}
    exp = make_experiment(client, rule=rule)
    eid = exp["id"]

    r1 = assign(client, eid, "req-1", "stu-young", profile={"age": 15})
    assert r1["outcome"] == "ineligible"
    assert r1["reason"] == "audience_rule_not_matched"
    assert any("age" in c for c in r1["reason_detail"]["failed_conditions"])

    # 资料后来变合格，首次事实仍不变（资格按首次申请时判定）
    client.post("/students", json={"student_key": "stu-young", "profile": {"age": 20}})
    r2 = assign(client, eid, "req-2", "stu-young")
    assert r2["outcome"] == "duplicate"
    assert r2["status"] == "ineligible"

    # 不合格者不计入分析（不凑组数）
    advance(client, 8 * DAY)
    client.post("/events", json={"events": [], "watermarks": [
        {"source": "orders", "complete_until": "2027-01-01T00:00:00Z"}]})
    res = run_analysis(client, eid)
    assert res["variants"]["control"]["total_assigned"] == 0
    assert res["variants"]["treatment"]["total_assigned"] == 0


def test_stop_only_blocks_new_assignments(client):
    exp = make_experiment(client)
    eid = exp["id"]
    r1 = assign(client, eid, "req-1", "stu-old")
    assert r1["outcome"] == "assigned"

    r = client.post(f"/experiments/{eid}/stop")
    assert r.json()["status"] == "stopped"

    # 新学员 → 拒绝并给原因
    r2 = assign(client, eid, "req-2", "stu-new")
    assert r2["outcome"] == "stopped" and r2["reason"] == "experiment_stopped"

    # 既有学员重复请求 → 仍返回原组（停招不影响既有分配）
    r3 = assign(client, eid, "req-3", "stu-old")
    assert r3["outcome"] == "duplicate" and r3["variant"] == r1["variant"]

    # 停招后不能再调比例
    r = client.post(f"/experiments/{eid}/ratio", json={"ratios": {"control": 1.0, "treatment": 0.0}})
    assert r.status_code == 409


def test_ratio_change_only_affects_new_students(client):
    exp = make_experiment(client, ratios={"control": 1.0, "treatment": 0.0})
    eid = exp["id"]
    for i in range(5):
        r = assign(client, eid, f"req-a{i}", f"stu-a{i}")
        assert r["variant"] == "control"

    client.post(f"/experiments/{eid}/ratio",
                json={"ratios": {"control": 0.0, "treatment": 1.0}})
    for i in range(5):
        r = assign(client, eid, f"req-b{i}", f"stu-b{i}")
        assert r["variant"] == "treatment"

    # 老学员仍属原组
    r = assign(client, eid, "req-a0-again", "stu-a0")
    assert r["variant"] == "control"

    # 分配记录归属各自比例配置期间
    detail = client.get(f"/experiments/{eid}").json()
    assert len(detail["ratio_configs"]) == 2


def test_exposure_failure_keeps_assignment(client):
    exp = make_experiment(client)
    eid = exp["id"]
    r1 = assign(client, eid, "req-1", "stu-x")
    assert r1["exposure_status"] == "unknown"

    r = client.post(f"/experiments/{eid}/exposures",
                    json={"student_key": "stu-x", "status": "failed"})
    assert r.status_code == 200
    assert r.json()["exposure_status"] == "failed"
    assert r.json()["status"] == "assigned"  # 分配事实保留

    # 未分配学员不能回执曝光
    r = client.post(f"/experiments/{eid}/exposures",
                    json={"student_key": "stu-nobody", "status": "success"})
    assert r.status_code == 404

    # 曝光失败者仍计入分析（ITT）
    advance(client, 8 * DAY)
    client.post("/events", json={"events": [], "watermarks": [
        {"source": "orders", "complete_until": "2027-01-01T00:00:00Z"}]})
    res = run_analysis(client, eid)
    total = sum(v["total_assigned"] for v in res["variants"].values())
    assert total == 1
