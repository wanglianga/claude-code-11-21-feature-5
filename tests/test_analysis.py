"""分析引擎：窗口边界、撤销/退款、快照、晚报、CI 门槛、卡方、对账、续算。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.services import variant_for

from conftest import (
    DAY,
    assign,
    advance,
    ingest,
    make_experiment,
    order_event,
    run_analysis,
    uniq,
)

T0 = "2026-01-01T00:00:00Z"  # 时钟默认起点


def _sections(client, run_id):
    r = client.get(f"/analysis/{run_id}/students", params={"limit": 10000})
    assert r.status_code == 200
    return {s["student_key"]: s for s in r.json()["students"]}


def test_window_left_closed_right_open(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-exact-end")      # 窗口 [01-01, 01-08)
    assign(client, eid, "r2", "stu-just-before")
    advance(client, 8 * DAY)  # 截止 01-09
    ingest(client, [
        order_event("stu-exact-end", "o1", 1, "2026-01-08T00:00:00Z"),   # 恰在结束时刻 → 不计入
        order_event("stu-just-before", "o2", 1, "2026-01-07T23:59:59Z"),  # 结束前 1 秒 → 计入
    ], [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])

    res = run_analysis(client, eid)
    secs = _sections(client, res["run_id"])
    assert secs["stu-exact-end"]["section"] == "mature_failure"
    assert secs["stu-just-before"]["section"] == "mature_success"


def test_cancel_and_refund_semantics(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    for i, k in enumerate(["stu-cancel", "stu-cancel-at-end", "stu-refund-late", "stu-reorder"]):
        assign(client, eid, f"r{i}", k)
    advance(client, 8 * DAY)
    ingest(client, [
        order_event("stu-cancel", "o1", 1, "2026-01-02T00:00:00Z"),
        order_event("stu-cancel", "o1", 2, "2026-01-03T00:00:00Z", event_type="order_canceled"),
        order_event("stu-cancel-at-end", "o2", 1, "2026-01-02T00:00:00Z"),
        # 恰在窗口结束时刻撤销 → 不属于"结束前撤销"，报名仍有效
        order_event("stu-cancel-at-end", "o2", 2, "2026-01-08T00:00:00Z", event_type="order_canceled"),
        order_event("stu-refund-late", "o3", 1, "2026-01-02T00:00:00Z"),
        # 窗口结束后才退款 → 不影响本次观察
        order_event("stu-refund-late", "o3", 2, "2026-01-09T00:00:00Z", event_type="order_refunded"),
        order_event("stu-reorder", "o4", 1, "2026-01-02T00:00:00Z"),
        order_event("stu-reorder", "o4", 2, "2026-01-03T00:00:00Z", event_type="order_canceled"),
        order_event("stu-reorder", "o5", 1, "2026-01-04T00:00:00Z"),  # 第二笔有效
    ], [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])

    res = run_analysis(client, eid)
    secs = _sections(client, res["run_id"])
    assert secs["stu-cancel"]["section"] == "mature_failure"
    assert secs["stu-cancel-at-end"]["section"] == "mature_success"
    assert secs["stu-refund-late"]["section"] == "mature_success"
    assert secs["stu-reorder"]["section"] == "mature_success"
    assert secs["stu-reorder"]["qualifying_biz_id"] == "o5"


def test_revocation_ingested_before_order(client):
    """撤销先到（摄取顺序乱序）：分析按发生时间评估，原单到齐后正确关联。"""
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-oos")
    advance(client, 8 * DAY)
    # 先摄取取消，后摄取订单
    ingest(client, [order_event("stu-oos", "o1", 2, "2026-01-03T00:00:00Z",
                                event_type="order_canceled")])
    ingest(client, [order_event("stu-oos", "o1", 1, "2026-01-02T00:00:00Z")],
           [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])
    res = run_analysis(client, eid)
    secs = _sections(client, res["run_id"])
    assert secs["stu-oos"]["section"] == "mature_failure"  # 订单在窗口内被撤销


def test_multiple_orders_counted_once(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-multi")
    advance(client, 8 * DAY)
    ingest(client, [
        order_event("stu-multi", "o1", 1, "2026-01-02T00:00:00Z"),
        order_event("stu-multi", "o2", 1, "2026-01-03T00:00:00Z"),
        order_event("stu-multi", "o3", 1, "2026-01-04T00:00:00Z"),
    ], [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])
    res = run_analysis(client, eid)
    secs = _sections(client, res["run_id"])
    assert secs["stu-multi"]["section"] == "mature_success"
    total_succ = sum(v["mature_success"] for v in res["variants"].values())
    assert total_succ == 1  # 多笔仅计一次


def test_observation_then_report_incomplete_then_mature(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-wait")

    # 第 3 天：窗口未结束 → 观察不足
    advance(client, 3 * DAY)
    res1 = run_analysis(client, eid)
    assert _sections(client, res1["run_id"])["stu-wait"]["section"] == "obs_insufficient"

    # 第 8 天：窗口已结束但无任何上报水位 → 数据等待
    advance(client, 5 * DAY)
    res2 = run_analysis(client, eid)
    assert _sections(client, res2["run_id"])["stu-wait"]["section"] == "report_incomplete"

    # 水位推进到恰等于窗口结束 → 覆盖，成熟
    ingest(client, [], [{"source": "orders", "complete_until": "2026-01-08T00:00:00Z"}])
    res3 = run_analysis(client, eid)
    assert _sections(client, res3["run_id"])["stu-wait"]["section"] == "mature_failure"


def test_late_events_only_affect_new_runs(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-late")
    advance(client, 8 * DAY)
    ingest(client, [], [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])

    run_a = run_analysis(client, eid)
    assert _sections(client, run_a["run_id"])["stu-late"]["section"] == "mature_failure"

    # 晚报：窗口内的订单现在才到（序号在快照之后）
    ingest(client, [order_event("stu-late", "o1", 1, "2026-01-02T00:00:00Z")])

    # 旧运行不变，仍可查询
    again = client.get(f"/analysis/{run_a['run_id']}").json()
    assert again["variants"] == run_a["variants"]
    assert _sections(client, run_a["run_id"])["stu-late"]["section"] == "mature_failure"

    # 新运行纳入晚报
    run_b = run_analysis(client, eid)
    assert run_b["event_seq_snapshot"] > run_a["event_seq_snapshot"]
    assert _sections(client, run_b["run_id"])["stu-late"]["section"] == "mature_success"


def test_ci_gating_and_interpretation(client):
    # 足够样本：两组成功/未成功均 >= 10 → 给出区间
    exp = make_experiment(client, seed="ci-seed", obs=7)
    eid = exp["id"]
    ratios = {"control": 0.5, "treatment": 0.5}
    keys = [f"stu-{i:03d}" for i in range(200)]
    for i, k in enumerate(keys):
        assign(client, eid, f"r{i}", k)
    advance(client, 8 * DAY)
    events = [
        order_event(k, f"o-{i}", 1, "2026-01-02T00:00:00Z")
        for i, k in enumerate(keys) if i % 2 == 0
    ]
    ingest(client, events, [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])
    res = run_analysis(client, eid)
    eff = res["effect"]
    assert eff["evidence_status"] == "ok"
    assert eff["ci_method"] == "normal_approximation_95"
    assert eff["ci_low"] < 0 < eff["ci_high"]
    assert "未区分" in eff["interpretation"]
    assert "不代表两方案等效" in eff["interpretation"]  # 跨零 ≠ 等效
    assert res["reconciled"] is True

    # 样本不足：只报计数与差值，不给区间
    exp2 = make_experiment(client, obs=7)
    eid2 = exp2["id"]
    for i in range(12):
        assign(client, eid2, f"s{i}", f"small-{i}")
    advance(client, 8 * DAY)
    res2 = run_analysis(client, eid2)
    eff2 = res2["effect"]
    assert eff2["evidence_status"] == "insufficient_counts"
    assert eff2["ci_low"] is None and eff2["ci_high"] is None
    assert "现在还不能判断" in eff2["interpretation"]


def _seed_assignments(session_factory, eid, cfg_id, variant, n, assigned_at):
    from app.models import Assignment
    with session_factory() as s:
        for i in range(n):
            s.add(Assignment(
                experiment_id=eid, student_key=uniq("seeded"), request_id=uniq("req"),
                status="assigned", variant=variant, ratio_config_id=cfg_id,
                assigned_at=assigned_at,
            ))
        s.commit()


def test_chi2_pause_on_imbalance(client):
    """分流人数显著偏离预设比例 → 暂停效果判断并列出异常来源。"""
    from app.db import get_session_factory

    exp = make_experiment(client, ratios={"control": 0.5, "treatment": 0.5})
    eid = exp["id"]
    cfg_id = client.get(f"/experiments/{eid}").json()["ratio_configs"][0]["id"]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _seed_assignments(get_session_factory(), eid, cfg_id, "control", 90, now)
    _seed_assignments(get_session_factory(), eid, cfg_id, "treatment", 10, now)

    advance(client, 8 * DAY)
    ingest(client, [], [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])
    res = run_analysis(client, eid)
    assert res["chi2"]["paused"] is True
    assert res["chi2"]["p_value"] < 0.001
    assert res["effect"]["evidence_status"] == "chi2_paused"
    assert res["effect"]["ci_low"] is None
    assert res["chi2"]["details"]  # 列出异常来源（期间 × 组别的偏差贡献）


def test_chi2_merged_across_ratio_periods(client):
    """比例调整：按各配置生效期间分别算期望再合并；不能拿最新比例检验整段历史。"""
    from app.db import get_session_factory

    exp = make_experiment(client, ratios={"control": 0.8, "treatment": 0.2})
    eid = exp["id"]
    cfg1 = client.get(f"/experiments/{eid}").json()["ratio_configs"][0]["id"]
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _seed_assignments(get_session_factory(), eid, cfg1, "control", 80, t1)
    _seed_assignments(get_session_factory(), eid, cfg1, "treatment", 20, t1)

    advance(client, 1 * DAY)
    client.post(f"/experiments/{eid}/ratio",
                json={"ratios": {"control": 0.2, "treatment": 0.8}})
    cfg2 = [c for c in client.get(f"/experiments/{eid}").json()["ratio_configs"]
            if c["id"] != cfg1][0]["id"]
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    _seed_assignments(get_session_factory(), eid, cfg2, "control", 20, t2)
    _seed_assignments(get_session_factory(), eid, cfg2, "treatment", 80, t2)

    # 合计 100/100，若误用最新比例 20/80 检验整段历史会误报；按期间合并则卡方≈0
    advance(client, 8 * DAY)
    ingest(client, [], [{"source": "orders", "complete_until": "2026-01-11T00:00:00Z"}])
    res = run_analysis(client, eid)
    assert res["chi2"]["paused"] is False
    assert res["chi2"]["stat"] < 1e-6
    assert res["chi2"]["df"] == 2  # 两个期间各贡献 1 个自由度


def test_reconciliation_across_sections(client):
    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    for i in range(4):  # A 组：窗口 01-08 结束 → 成熟
        assign(client, eid, f"ra{i}", f"stu-a{i}")
    advance(client, 1 * DAY)
    for i in range(4):  # B 组：窗口 01-09 结束 ≤ 截止，但水位只到 01-08 12:00 → 上报未齐
        assign(client, eid, f"rb{i}", f"stu-b{i}")
    advance(client, 1 * DAY)
    for i in range(4):  # C 组：窗口 01-10 结束 > 截止 → 观察不足
        assign(client, eid, f"rc{i}", f"stu-c{i}")
    advance(client, 6 * DAY)  # 截止 01-09

    a_keys = [f"stu-a{i}" for i in range(4)]
    ingest(client,
           [order_event(k, f"oa{i}", 1, "2026-01-02T00:00:00Z") for i, k in enumerate(a_keys[:2])],
           [{"source": "orders", "complete_until": "2026-01-08T12:00:00Z"}])
    res = run_analysis(client, eid)

    totals = {k: sum(v[k] for v in res["variants"].values())
              for k in ("total_assigned", "obs_insufficient", "report_incomplete",
                        "mature", "mature_success", "mature_failure")}
    assert totals["total_assigned"] == 12
    assert totals["obs_insufficient"] == 4
    assert totals["report_incomplete"] == 4
    assert totals["mature"] == 4
    assert totals["mature_success"] == 2
    assert totals["mature_failure"] == 2
    for v in res["variants"].values():
        assert v["total_assigned"] == v["obs_insufficient"] + v["report_incomplete"] + v["mature"]
        assert v["mature"] == v["mature_success"] + v["mature_failure"]
    assert res["reconciled"] is True


def test_resume_interrupted_run_keeps_same_cutoff(client):
    from app.analysis import create_run
    from app.db import get_session_factory

    exp = make_experiment(client, obs=7)
    eid = exp["id"]
    assign(client, eid, "r1", "stu-resume")
    advance(client, 8 * DAY)
    ingest(client, [order_event("stu-resume", "o1", 1, "2026-01-02T00:00:00Z")],
           [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])

    # 模拟进程中断：运行已创建（快照冻结）但计算未完成
    with get_session_factory()() as s:
        run = create_run(s, eid)
        run_id, cutoff, seq = run.id, run.cutoff, run.event_seq_snapshot

    advance(client, 3 * DAY)  # 中断期间时钟继续走
    r = client.post(f"/analysis/{run_id}/resume")
    assert r.status_code == 200
    res = r.json()
    assert res["status"] == "done"
    assert res["cutoff"] == cutoff.isoformat()  # 同一批次：截止点不变
    assert res["event_seq_snapshot"] == seq
    assert _sections(client, run_id)["stu-resume"]["section"] == "mature_success"

    # 已完成的运行再 resume → 幂等返回
    again = client.post(f"/analysis/{run_id}/resume").json()
    assert again["variants"] == res["variants"]


def test_student_view_basis(client):
    exp = make_experiment(client, obs=7,
                          rule={"conditions": [{"field": "age", "op": "gte", "value": 18}]})
    eid = exp["id"]
    assign(client, eid, "r1", "stu-view", profile={"age": 22})
    client.post(f"/experiments/{eid}/exposures",
                json={"student_key": "stu-view", "status": "success"})
    advance(client, 8 * DAY)
    ingest(client, [order_event("stu-view", "o-9", 1, "2026-01-02T00:00:00Z")],
           [{"source": "orders", "complete_until": "2026-01-09T00:00:00Z"}])
    run = run_analysis(client, eid)

    r = client.get(f"/experiments/{eid}/students/stu-view")
    assert r.status_code == 200
    view = r.json()
    a = view["assignment"]
    assert a["status"] == "assigned" and a["variant"] in ("control", "treatment")
    assert a["eligibility"]["eligible"] is True
    assert a["eligibility"]["profile"]["age"] == 22  # 资格快照：首次申请时的资料
    assert a["exposure_status"] == "success"
    m = view["maturity_now"]
    assert m["section"] == "mature"
    assert m["window_start"] == "2026-01-01T00:00:00+00:00"
    assert m["window_end"] == "2026-01-08T00:00:00+00:00"
    basis = view["enrollment_basis_now"]
    assert basis["enrolled"] is True
    assert basis["qualifying_order"]["biz_id"] == "o-9"
    assert basis["basis_events"][0]["event_type"] == "order_created"
    assert view["runs"][0]["run_id"] == run["run_id"]
    assert view["runs"][0]["section"] == "mature_success"

    # 无记录学员 → 404
    r = client.get(f"/experiments/{eid}/students/stu-nobody")
    assert r.status_code == 404
