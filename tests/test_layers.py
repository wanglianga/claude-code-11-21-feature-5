"""互斥层 / 容量区间 / 跨层共存专项测试。

覆盖：
- 层内区间重叠与总额校验、revision 乐观锁
- 同层互斥（一人至多一个实验）、桶位稳定、先选层内实验再分组
- 三类未入组原因分别落库：受众不合格 / 层内未命中 / 实验停止
- 扩流只增不减、老成员不换组、新边界不重判旧决定
- 停招释放范围仅新人可用、旧成员完成观察
- 两个管理员并发争抢最后容量只有一个生效、总容量恒成立
- 重放走当时路由（route_snapshot 冻结），不能用新边界解释旧入组
- 跨层共同曝光报告与分析运行的分层声明快照
"""
from __future__ import annotations

import threading

from conftest import uniq

# ---------------------------------------------------------------- 助手


def make_layer(client, *, seed=None, total=1000):
    r = client.post("/layers", json={
        "name": uniq("layer"), "seed": seed or uniq("lseed"), "total_buckets": total,
    })
    assert r.status_code == 201, r.text
    return r.json()


def make_layer_experiment(client, layer_id, *, seed=None, ratios=None, rule=None):
    body = {
        "name": uniq("lexp"), "seed": seed or uniq("eseed"),
        "observation_days": 7,
        "ratios": ratios or {"control": 0.5, "treatment": 0.5},
        "layer_id": layer_id,
    }
    if rule is not None:
        body["audience_rule"] = rule
    r = client.post("/experiments", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def attach(client, layer_id, exp_id, lo, hi, revision):
    r = client.post(f"/layers/{layer_id}/experiments/{exp_id}/slot", json={
        "bucket_from": lo, "bucket_to": hi, "expected_revision": revision,
    })
    return r


def expand(client, layer_id, exp_id, lo, hi, revision):
    return client.put(f"/layers/{layer_id}/experiments/{exp_id}/capacity", json={
        "bucket_from": lo, "bucket_to": hi, "expected_revision": revision,
    })


def assign(client, eid, req, key, **kw):
    body = {"request_id": req, "student_key": key, **kw}
    r = client.post(f"/experiments/{eid}/assignments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def current_revision(client, layer_id):
    return client.get(f"/layers/{layer_id}").json()["revision"]


# ---------------------------------------------------------------- 1. 容量校验

def test_slot_overlap_and_total_validation(client):
    layer = make_layer(client, total=1000)
    lid = layer["id"]
    e1 = make_layer_experiment(client, lid)
    e2 = make_layer_experiment(client, lid)

    r = attach(client, lid, e1["id"], 0, 600, 0)
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1
    detail = client.get(f"/layers/{lid}").json()
    assert detail["capacity"] == {"used": 600, "free": 400, "total": 1000}

    # 区间重叠 → 409
    r = attach(client, lid, e2["id"], 500, 800, 1)
    assert r.status_code == 409 and "重叠" in r.json()["detail"]

    # 总额超限 → 409
    r = attach(client, lid, e2["id"], 600, 1100, 1)
    assert r.status_code == 400

    # 陈旧 revision → 409（乐观锁）
    r = attach(client, lid, e2["id"], 600, 900, 0)
    assert r.status_code == 409 and "revision" in r.json()["detail"]

    # 合法区间生效，revision 再 +1
    r = attach(client, lid, e2["id"], 600, 900, 1)
    assert r.status_code == 200 and r.json()["revision"] == 2
    detail = client.get(f"/layers/{lid}").json()
    assert detail["capacity"]["used"] == 900 and detail["capacity"]["free"] == 100


def test_illegal_interval_shape(client):
    layer = make_layer(client)
    e = make_layer_experiment(client, layer["id"])
    # from >= to
    r = attach(client, layer["id"], e["id"], 500, 500, 0)
    assert r.status_code == 400
    r = attach(client, layer["id"], e["id"], 800, 300, 0)
    assert r.status_code == 400


# ---------------------------------------------------------------- 2. 同层互斥与稳定桶位

def test_same_layer_mutual_exclusion_and_stable_bucket(client):
    from app.layers import layer_bucket

    layer = make_layer(client, seed="salt-x", total=1000)
    lid = layer["id"]
    e1 = make_layer_experiment(client, lid, seed="exp-a")
    e2 = make_layer_experiment(client, lid, seed="exp-b")
    assert attach(client, lid, e1["id"], 0, 500, 0).status_code == 200
    assert attach(client, lid, e2["id"], 500, 1000, 1).status_code == 200

    keys = [f"stu-{i:04d}" for i in range(60)]
    owners: dict[str, int] = {}
    for k in keys:
        bucket = layer_bucket("salt-x", k, 1000)
        res1 = assign(client, e1["id"], f"r-a-{k}", k)
        res2 = assign(client, e2["id"], f"r-b-{k}", k)
        # 桶位与响应一致
        assert res1["layer_bucket"] == bucket and res2["layer_bucket"] == bucket
        if bucket < 500:
            assert res1["outcome"] == "assigned" and res1["status"] == "assigned"
            assert res2["outcome"] == "layer_not_selected"
            owners[k] = e1["id"]
        else:
            assert res2["outcome"] == "assigned"
            assert res1["outcome"] == "layer_not_selected"
            owners[k] = e2["id"]
        # 层内一人至多一个实验：两个请求不可能同时 assigned
        assert not (res1["status"] == "assigned" and res2["status"] == "assigned")

    # 桶位稳定：重复请求/换 request_id 都返回首次事实
    k0 = keys[0]
    again = assign(client, e1["id"], f"r-a-{k0}-dup", k0)
    assert again["outcome"] == "duplicate"
    first = assign(client, e1["id"], f"r-a-{k0}", k0)
    assert again["variant"] == first["variant"]
    assert again["route_snapshot"]["layer"]["revision"] == first["route_snapshot"]["layer"]["revision"]


# ---------------------------------------------------------------- 3. 三类未入组原因

def test_three_distinct_non_selection_reasons(client):
    layer = make_layer(client, total=1000)
    lid = layer["id"]
    rule = {"conditions": [{"field": "age", "op": "gte", "value": 18}]}
    e = make_layer_experiment(client, lid, rule=rule)
    assert attach(client, lid, e["id"], 0, 500, 0).status_code == 200

    # (a) 层内未命中：桶位落在 [500,1000)
    from app.layers import layer_bucket
    miss_key = next(
        f"miss-{i}" for i in range(500)
        if layer_bucket(layer["seed"], f"miss-{i}", 1000) >= 500
    )
    r_miss = assign(client, e["id"], "req-miss", miss_key, profile={"age": 25})
    assert r_miss["status"] == "layer_not_selected"
    assert r_miss["reason"] == "layer_not_selected"
    assert r_miss["reason_detail"]["bucket"] >= 500
    assert r_miss["route_revision"] == 1

    # (b) 受众不合格：先找一个命中区间的学员
    hit_key = next(
        f"hit-{i}" for i in range(500)
        if layer_bucket(layer["seed"], f"hit-{i}", 1000) < 500
    )
    r_bad = assign(client, e["id"], "req-bad", hit_key, profile={"age": 15})
    assert r_bad["status"] == "ineligible"
    assert r_bad["reason"] == "audience_rule_not_matched"
    assert r_bad["eligibility"]["eligible"] is False

    # (c) 实验停止：另找一名命中区间且合格的学员先入组
    ok_key = next(
        f"ok-{i}" for i in range(2000)
        if layer_bucket(layer["seed"], f"ok-{i}", 1000) < 500 and f"ok-{i}" != hit_key
    )
    r_ok = assign(client, e["id"], "req-ok", ok_key, profile={"age": 20})
    assert r_ok["outcome"] == "assigned"
    client.post(f"/experiments/{e['id']}/stop")
    new_key = next(
        f"new-{i}" for i in range(2000)
        if layer_bucket(layer["seed"], f"new-{i}", 1000) < 500
    )
    r_stop = assign(client, e["id"], "req-stop", new_key, profile={"age": 20})
    assert r_stop["status"] == "stopped" and r_stop["reason"] == "experiment_stopped"

    # 三种原因各自独立可统计，不用一条失败代替
    detail = client.get(f"/experiments/{e['id']}").json()["assignment_counts"]
    assert detail["layer_not_selected"] >= 1
    assert detail["ineligible"] >= 1
    assert detail["stopped"] >= 1


def test_explain_endpoint(client):
    from app.layers import layer_bucket

    layer = make_layer(client, total=1000)
    lid = layer["id"]
    e = make_layer_experiment(client, lid)
    assert attach(client, lid, e["id"], 0, 300, 0).status_code == 200

    hit_key = next(f"h-{i}" for i in range(500)
                   if layer_bucket(layer["seed"], f"h-{i}", 1000) < 300)
    miss_key = next(f"m-{i}" for i in range(500)
                    if layer_bucket(layer["seed"], f"m-{i}", 1000) >= 300)

    r = client.get(f"/layers/{lid}/explain", params={"student_key": hit_key})
    assert r.status_code == 200
    body = r.json()
    assert body["selected_experiment_id"] == e["id"] and body["reason"] is None

    r = client.get(f"/layers/{lid}/explain", params={"student_key": miss_key})
    body = r.json()
    assert body["selected_experiment_id"] is None
    assert body["reason"] == "layer_not_selected"
    assert body["enrolling_intervals"][0]["contains_bucket"] is False


# ---------------------------------------------------------------- 4. 扩流

def test_expansion_uses_free_space_and_never_moves_old_members(client):
    from app.layers import layer_bucket

    layer = make_layer(client, total=1000)
    lid = layer["id"]
    e = make_layer_experiment(client, lid)
    assert attach(client, lid, e["id"], 0, 300, 0).status_code == 200

    # 初始区间内的老成员先入组
    old_keys = [f"old-{i}" for i in range(40)]
    old_decisions = {}
    for k in old_keys:
        res = assign(client, e["id"], f"req-{k}", k)
        if res["status"] == "assigned":
            old_decisions[k] = res["variant"]
    assert old_decisions  # 至少有人命中

    # 扩流到 [0,700)：包含旧区间
    r = expand(client, lid, e["id"], 0, 700, 1)
    assert r.status_code == 200, r.text
    assert r.json()["layer"]["capacity"]["used"] == 700

    # 拒绝缩容/平移（新边界不包含旧区间 → 会驱逐老成员）
    r = expand(client, lid, e["id"], 100, 700, 2)
    assert r.status_code == 409 and "驱逐" in r.json()["detail"]
    r = expand(client, lid, e["id"], 0, 600, 2)
    assert r.status_code == 409

    # 老成员不换组
    for k, variant in old_decisions.items():
        again = assign(client, e["id"], f"req-{k}-2", k)
        assert again["outcome"] == "duplicate" and again["variant"] == variant

    # 新范围 [300,700) 的学员此前无法进入，现在可以（新来者使用新增空间）
    new_key = next(
        f"fresh-{i}" for i in range(3000)
        if 300 <= layer_bucket(layer["seed"], f"fresh-{i}", 1000) < 700
    )
    res = assign(client, e["id"], "req-fresh", new_key)
    assert res["status"] == "assigned"
    assert res["route_revision"] == 2  # 绑定扩流后的路由

    # 层内未命中的首次决定也不被新边界重判：找一个在 rev1 时未命中的学员
    miss_key = next(
        f"frozen-{i}" for i in range(5000)
        if 300 <= layer_bucket(layer["seed"], f"frozen-{i}", 1000) < 700
    )
    before = assign(client, e["id"], "req-frozen-1", miss_key)
    # 该学员桶位在扩流后属于本实验，但首次事实已冻结为 layer_not_selected
    assert before["status"] == "assigned"  # 扩流发生在请求之前 → 正常入组
    # 反向验证：先制造未命中，再扩流覆盖，旧决定不变
    far_key = next(
        f"far-{i}" for i in range(8000)
        if layer_bucket(layer["seed"], f"far-{i}", 1000) >= 700
    )
    first = assign(client, e["id"], "req-far-1", far_key)
    assert first["status"] == "layer_not_selected"
    assert expand(client, lid, e["id"], 0, 1000, 2).status_code == 200
    replay = assign(client, e["id"], "req-far-2", far_key)
    assert replay["outcome"] == "duplicate"
    assert replay["status"] == "layer_not_selected"  # 新边界不重判旧入组


# ---------------------------------------------------------------- 5. 停招释放

def test_stop_releases_slot_for_newcomers_only(client):
    from app.layers import layer_bucket

    layer = make_layer(client, total=1000)
    lid = layer["id"]
    a = make_layer_experiment(client, lid, seed="exp-a")
    b = make_layer_experiment(client, lid, seed="exp-b")
    assert attach(client, lid, a["id"], 0, 400, 0).status_code == 200

    old_key = next(f"old-{i}" for i in range(1000)
                   if layer_bucket(layer["seed"], f"old-{i}", 1000) < 400)
    old = assign(client, a["id"], "req-old", old_key)
    assert old["status"] == "assigned"
    old_variant = old["variant"]

    # 停招：层内区间释放，revision 推进，容量归零可再分配
    assert client.post(f"/experiments/{a['id']}/stop").status_code == 200
    detail = client.get(f"/layers/{lid}").json()
    stopped_row = next(x for x in detail["experiments"] if x["experiment_id"] == a["id"])
    assert stopped_row["status"] == "stopped"
    assert detail["capacity"]["used"] == 0 and detail["capacity"]["free"] == 1000

    # 旧成员仍完成观察：重复请求返回原组
    again = assign(client, a["id"], "req-old-2", old_key)
    assert again["outcome"] == "duplicate" and again["variant"] == old_variant

    # 新人申请已停实验 → stopped 原因
    new_key = next(f"new-{i}" for i in range(3000)
                   if layer_bucket(layer["seed"], f"new-{i}", 1000) < 400)
    r = assign(client, a["id"], "req-new-a", new_key)
    assert r["status"] == "stopped"

    # 释放范围可被新实验申请，同一新人进入新实验
    rev = current_revision(client, lid)
    assert attach(client, lid, b["id"], 0, 400, rev).status_code == 200
    r = assign(client, b["id"], "req-new-b", new_key)
    assert r["status"] == "assigned"
    # 老成员不会被卷入新实验（同层互斥 + 首次事实）
    r = assign(client, b["id"], "req-old-b", old_key)
    assert r["status"] == "layer_not_selected"
    assert r["reason_detail"]["sticky_member_of_experiment"] == a["id"]

    # explain 端点如实解释老成员黏性
    ex = client.get(f"/layers/{lid}/explain", params={"student_key": old_key}).json()
    assert ex["prior_assignment"] is not None
    assert ex["prior_assignment"]["experiment_id"] == a["id"]
    assert "黏性" in ex["detail"]


# ---------------------------------------------------------------- 6. 并发争抢最后容量

def test_concurrent_claim_for_last_capacity_single_winner():
    """两个管理员并发申请同一段剩余容量：行锁 + revision 乐观锁，只接受一个修订。"""
    from app.db import get_session_factory
    from app import layers as layer_svc

    factory = get_session_factory()
    with factory() as s:
        layer = layer_svc.create_layer(s, name=uniq("race-layer"), seed="race-salt",
                                       total_buckets=1000)
        lid = layer.id
        e_a = _create_bound_experiment(s, "race-a", lid)
        e_b = _create_bound_experiment(s, "race-b", lid)
        e_c = _create_bound_experiment(s, "race-c", lid)
        # A 先占 [0,900)，剩最后 100
        layer_svc.attach_experiment(s, layer, e_a, bucket_from=0, bucket_to=900,
                                    expected_revision=0)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def claim(exp_id: int):
        with factory() as s:
            from app.models import Experiment
            barrier.wait()  # 尽量让两个事务同时进入
            try:
                target = s.get(Experiment, exp_id)
                layer_svc.attach_experiment(
                    s, layer_svc.get_layer(s, lid), target,
                    bucket_from=900, bucket_to=1000, expected_revision=1,
                )
                outcomes.append("win")
            except layer_svc.DomainError as e:
                outcomes.append(f"lose:{e.status}")

    t1 = threading.Thread(target=claim, args=(e_b.id,))
    t2 = threading.Thread(target=claim, args=(e_c.id,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes) == ["lose:409", "win"]
    with factory() as s:
        detail = layer_svc.layer_detail(s, layer_svc.get_layer(s, lid))
        # 总容量恒成立：只用了 1000，没有超卖
        assert detail["capacity"] == {"used": 1000, "free": 0, "total": 1000}
        enrolling = [x for x in detail["experiments"] if x["status"] == "enrolling"]
        assert len(enrolling) == 2
        # 区间两两不重叠
        spans = sorted((x["bucket_from"], x["bucket_to"]) for x in enrolling)
        assert spans == [(0, 900), (900, 1000)]


def _create_bound_experiment(session, name_seed: str, layer_id: int):
    from app import services
    return services.create_experiment(
        session,
        name=uniq(name_seed), seed=name_seed, observation_days=7,
        ratios={"control": 0.5, "treatment": 0.5}, layer_id=layer_id,
    )


def test_capacity_invariant_through_mixed_operations(client):
    """挂载/扩流/停招/再挂载混合操作后，在招区间互不重叠且 used+free=total。"""
    layer = make_layer(client, total=500)
    lid = layer["id"]
    e1 = make_layer_experiment(client, lid)
    e2 = make_layer_experiment(client, lid)
    e3 = make_layer_experiment(client, lid)

    rev = current_revision(client, lid)
    assert attach(client, lid, e1["id"], 0, 100, rev).status_code == 200
    rev = current_revision(client, lid)
    assert attach(client, lid, e2["id"], 200, 300, rev).status_code == 200
    rev = current_revision(client, lid)
    assert expand(client, lid, e1["id"], 0, 200, rev).status_code == 200  # 与 e2 不重叠
    client.post(f"/experiments/{e2['id']}/stop")
    rev = current_revision(client, lid)
    assert attach(client, lid, e3["id"], 200, 450, rev).status_code == 200

    detail = client.get(f"/layers/{lid}").json()
    cap = detail["capacity"]
    assert cap["used"] + cap["free"] == cap["total"] == 500
    enrolling = sorted(
        (x["bucket_from"], x["bucket_to"])
        for x in detail["experiments"] if x["status"] == "enrolling"
    )
    # [0,200) 与 [200,450) 相邻不重叠
    assert enrolling == [(0, 200), (200, 450)]


# ---------------------------------------------------------------- 7. 历史路由重放

def test_replay_uses_historical_route_snapshot(client):
    layer = make_layer(client, total=1000)
    lid = layer["id"]
    e = make_layer_experiment(client, lid)
    assert attach(client, lid, e["id"], 0, 200, 0).status_code == 200

    r1 = assign(client, e["id"], "req-hist", "stu-hist")
    assert r1["route_revision"] == 1
    snap1 = r1["route_snapshot"]
    assert snap1["layer"]["revision"] == 1
    assert len(snap1["routing"]) == 1 and snap1["routing"][0]["bucket_to"] == 200

    # 修订历史可逐版查询
    revs = client.get(f"/layers/{lid}/revisions").json()
    assert [r["revision"] for r in revs] == [0, 1]
    old = client.get(f"/layers/{lid}/revisions/1").json()
    assert old["snapshot"][0]["bucket_to"] == 200
    # 快照内每行自带其所属修订版本（创建即 rev1），不滞后
    assert old["snapshot"][0]["revision"] == 1
    assert old["snapshot"][0]["status"] == "enrolling"

    # 扩流到 [0,600)
    assert expand(client, lid, e["id"], 0, 600, 1).status_code == 200

    # 重放历史请求：返回原决定，仍引用 revision=1 的当时路由
    replay = assign(client, e["id"], "req-hist", "stu-hist")
    assert replay["idempotent_replay"] is True
    assert replay["route_revision"] == 1
    assert replay["route_snapshot"]["routing"][0]["bucket_to"] == 200
    assert replay["route_snapshot"] == snap1

    # 旧 revision 快照不被新边界改写；rev2 快照反映新区间且行 revision=2
    old_again = client.get(f"/layers/{lid}/revisions/1").json()
    assert old_again["snapshot"] == old["snapshot"]
    new_snap = client.get(f"/layers/{lid}/revisions/2").json()
    assert new_snap["snapshot"][0]["bucket_to"] == 600
    assert new_snap["snapshot"][0]["revision"] == 2


# ---------------------------------------------------------------- 8. 跨层共存与报告

def test_cross_layer_co_exposure_and_report(client):
    l1 = make_layer(client, seed="layer-one", total=1000)
    l2 = make_layer(client, seed="layer-two", total=1000)
    e1 = make_layer_experiment(client, l1["id"], seed="e1")
    e2 = make_layer_experiment(client, l2["id"], seed="e2")
    assert attach(client, l1["id"], e1["id"], 0, 1000, 0).status_code == 200
    assert attach(client, l2["id"], e2["id"], 0, 1000, 0).status_code == 200

    # 同一学员在两个共存层各入组并曝光（系统记录事实，不承诺互不影响）
    for k in ("co-1", "co-2", "co-3"):
        r1 = assign(client, e1["id"], f"a-{k}", k)
        r2 = assign(client, e2["id"], f"b-{k}", k)
        assert r1["status"] == "assigned" and r2["status"] == "assigned"
        client.post(f"/experiments/{e1['id']}/exposures",
                    json={"student_key": k, "status": "success"})
        client.post(f"/experiments/{e2['id']}/exposures",
                    json={"student_key": k, "status": "success"})

    # 曝光回执幂等：共同曝光不重复计数
    client.post(f"/experiments/{e1['id']}/exposures",
                json={"student_key": "co-1", "status": "failed"})

    report = client.get("/reports/co-exposures").json()
    pair = next(p for p in report["pairs"]
                if {p["experiment_a"]["experiment_id"],
                    p["experiment_b"]["experiment_id"]} == {e1["id"], e2["id"]})
    assert pair["overlap_students"] == 3
    assert pair["both_exposure_success"] == 3
    assert pair["by_variant_pair"]  # 按共同分组拆分人数
    assert "不支持事后剔除" in pair["note"]

    # 学员视图可回看跨层曝光
    view = client.get(f"/experiments/{e1['id']}/students/co-1").json()
    assert view["co_exposures"]["distinct_layers"] == 2
    assert len(view["co_exposures"]["co_exposures"]) == 2

    # 同层互斥：报告中不出现同层实验对
    for p in report["pairs"]:
        assert p["experiment_a"]["layer_id"] != p["experiment_b"]["layer_id"]


def test_analysis_run_freezes_layer_declaration(client):
    layer = make_layer(client, total=1000)
    lid = layer["id"]
    e = make_layer_experiment(client, lid)
    assert attach(client, lid, e["id"], 0, 300, 0).status_code == 200
    assign(client, e["id"], "req-1", "stu-run-1")

    run = client.post(f"/experiments/{e['id']}/analysis").json()
    assert run["layer_snapshot"] is not None
    assert run["layer_snapshot"]["layer"]["revision"] == 1
    declared = run["layer_snapshot"]["routing"]
    assert declared[0]["bucket_to"] == 300
    assert "预先声明" in run["layer_snapshot"]["declared_note"]

    # 扩流后旧运行的分层声明保持冻结
    assert expand(client, lid, e["id"], 0, 800, 1).status_code == 200
    old_run = client.get(f"/analysis/{run['run_id']}").json()
    assert old_run["layer_snapshot"]["routing"][0]["bucket_to"] == 300

    new_run = client.post(f"/experiments/{e['id']}/analysis").json()
    assert new_run["layer_snapshot"]["layer"]["revision"] == 2
    assert new_run["layer_snapshot"]["routing"][0]["bucket_to"] == 800


# ---------------------------------------------------------------- 9. 无层实验不受影响

def test_independent_experiment_without_layer(client):
    r = client.post("/experiments", json={
        "name": uniq("solo"), "seed": "solo-seed", "observation_days": 7,
        "ratios": {"control": 0.5, "treatment": 0.5},
    })
    assert r.status_code == 201
    eid = r.json()["id"]
    res = assign(client, eid, "req-solo", "stu-solo")
    assert res["status"] == "assigned"
    assert res["layer_id"] is None and res["route_snapshot"] is None
    run = client.post(f"/experiments/{eid}/analysis").json()
    assert run["layer_snapshot"] is None


def test_layer_binding_edges(client):
    # 绑定不存在的层 → 404
    r = client.post("/experiments", json={
        "name": uniq("ghost"), "seed": "g", "observation_days": 7,
        "ratios": {"control": 0.5, "treatment": 0.5}, "layer_id": 999999,
    })
    assert r.status_code == 404

    # 建在层内但从未申请区间：停招不报错，新学员得到 stopped 原因
    layer = make_layer(client)
    e = make_layer_experiment(client, layer["id"])
    r = client.post(f"/experiments/{e['id']}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    res = assign(client, e["id"], "req-x", "stu-noslot")
    assert res["status"] == "stopped"
    # 层 revision 未因无行可释放而变化
    assert client.get(f"/layers/{layer['id']}").json()["revision"] == 0

    # 不能把同一实验挂到第二个层
    other = make_layer(client)
    r = client.post(f"/layers/{other['id']}/experiments/{e['id']}/slot",
                    json={"bucket_from": 0, "bucket_to": 100, "expected_revision": 0})
    assert r.status_code == 400
