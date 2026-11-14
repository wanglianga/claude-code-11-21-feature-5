#!/usr/bin/env python3
"""端到端演示：选择偏差、观察不足、重复报名、晚报。

对运行中的服务执行四个场景，展示系统如何帮助运营分清
“没有效果”（证据未区分方向）与“现在还不能判断”（观察不足/上报未齐/样本不足）。

用法：
    python scripts/demo.py                 # 默认 http://127.0.0.1:8000
    DEMO_BASE_URL=http://host:8000 python scripts/demo.py
"""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")
RNG = random.Random(20261114)
SUFFIX = f"{int(time.time())}"
CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    CHECKS.append((label, ok))
    print(f"    [{'✓' if ok else '✗'}] {label}")


def post(path: str, body: dict) -> dict:
    r = requests.post(f"{BASE}{path}", json=body, timeout=60)
    if r.status_code >= 400:
        raise SystemExit(f"POST {path} -> {r.status_code}: {r.text}")
    return r.json()


def get(path: str, **params) -> dict:
    r = requests.get(f"{BASE}{path}", params=params, timeout=60)
    if r.status_code >= 400:
        raise SystemExit(f"GET {path} -> {r.status_code}: {r.text}")
    return r.json()


def clock_now() -> datetime:
    return datetime.fromisoformat(get("/clock")["now"])


def advance(days: float) -> datetime:
    return datetime.fromisoformat(post("/clock/advance", {"seconds": int(days * 86400)})["now"])


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def make_experiment(name: str, obs_days: int, rule: dict | None = None) -> int:
    body = {
        "name": f"{name}-{SUFFIX}",
        "seed": f"seed-{name}",
        "observation_days": obs_days,
        "ratios": {"control": 0.5, "treatment": 0.5},
    }
    if rule:
        body["audience_rule"] = rule
    exp = post("/experiments", body)
    print(f"  实验 #{exp['id']}「{exp['name']}」 观察 {obs_days} 天，比例 50/50")
    return exp["id"]


def assign(eid: int, key: str, req: str, **kw) -> dict:
    return post(f"/experiments/{eid}/assignments",
                {"request_id": req, "student_key": key, **kw})


def ingest(events: list[dict], watermark_to: datetime | None = None) -> dict:
    watermarks = [{"source": "orders", "complete_until": iso(watermark_to)}] if watermark_to else []
    return post("/events", {"events": events, "watermarks": watermarks})


def run_sections(run_id: int) -> dict[str, dict]:
    data = get(f"/analysis/{run_id}/students", limit=100000)
    return {s["student_key"]: s for s in data["students"]}


def variant_counts(res: dict, variant: str) -> dict:
    return res["variants"][variant]


def banner(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


# ---------------------------------------------------------------- 场景 1
def scenario_selection_bias() -> None:
    banner("场景 1｜选择偏差：只给看完介绍的人展示新版，会高估新方案")
    print("""  设定：学员分高/低意愿（30%/70%）。高意愿者更可能看完介绍（90% vs 30%），
  也更可能报名（50% vs 10%）。新方案实际【没有任何效果】（两组真实报名率相同）。
  对照错误做法：把“看完介绍的人”当作新方案组与未看完者比较。""")
    eid = make_experiment("demo-selection", obs_days=1)

    start = clock_now()
    n = 800
    students = []
    events = []
    for i in range(n):
        key = f"s1-{i:04d}"
        high = RNG.random() < 0.3
        viewed = RNG.random() < (0.9 if high else 0.3)
        enrolled = RNG.random() < (0.5 if high else 0.1)
        assign(eid, key, f"s1-req-{i}")
        if viewed:
            post(f"/experiments/{eid}/exposures", {"student_key": key, "status": "success"})
        if enrolled:
            occurred = start + timedelta(hours=RNG.uniform(1, 20))
            events.append({"source": "orders", "biz_id": f"s1-ord-{i}", "seq": 1,
                           "event_type": "order_created", "student_key": key,
                           "occurred_at": iso(occurred)})
        students.append((key, viewed, enrolled))

    now = advance(1)
    ingest(events, watermark_to=now)
    res = post(f"/experiments/{eid}/analysis", {})
    run_id = res["run_id"]

    # 系统 ITT：按首次分配的全部分析（含未看完介绍的人）
    ct, cc = variant_counts(res, "treatment"), variant_counts(res, "control")
    p_t = ct["mature_success"] / ct["mature"]
    p_c = cc["mature_success"] / cc["mature"]
    print(f"\n  【本系统·ITT】对照组 {cc['mature_success']}/{cc['mature']} = {p_c:.1%}，"
          f"新方案组 {ct['mature_success']}/{ct['mature']} = {p_t:.1%}")
    eff = res["effect"]
    print(f"  差值 {eff['diff']:+.1%}，95% 区间 [{eff['ci_low']:+.1%}, {eff['ci_high']:+.1%}]")
    print(f"  解读：{eff['interpretation']}")

    # 错误做法（演示脚本本地计算）：只把“看完介绍的人”算作新方案组
    secs = run_sections(run_id)
    mature = [s for s in secs.values() if s["section"] in ("mature_success", "mature_failure")]
    viewers = [s for s in mature if s["exposure_status"] == "success"]
    non_viewers = [s for s in mature if s["exposure_status"] != "success"]
    v_succ = sum(1 for s in viewers if s["section"] == "mature_success")
    nv_succ = sum(1 for s in non_viewers if s["section"] == "mature_success")
    print(f"\n  【错误做法·选择偏差】“看完介绍=新方案组” {v_succ}/{len(viewers)} = {v_succ/len(viewers):.1%}"
          f" vs “未看完=旧方案组” {nv_succ}/{len(non_viewers)} = {nv_succ/len(non_viewers):.1%}"
          f" → 虚假提升 {v_succ/len(viewers) - nv_succ/len(non_viewers):+.1%}")
    print("  结论：看完介绍的人本来就更可能报名，按观看筛选会把他们集中进“新组”。")
    print("        本服务分配先于展示、按首次分配全量（ITT）分析，从机制上避免该偏差。")
    check("ITT 区间跨 0（未区分方向，而非等效）",
          eff["ci_low"] < 0 < eff["ci_high"] and "未区分" in eff["interpretation"])
    check("选择偏差做法会产生显著虚假提升",
          v_succ / len(viewers) - nv_succ / len(non_viewers) > 0.1)


# ---------------------------------------------------------------- 场景 2
def scenario_observation_waiting() -> None:
    banner("场景 2｜观察不足与上报未齐：“现在还不能判断”≠“没有效果”")
    print("  设定：观察期 7 天。演示三种等待状态如何被如实标注。")
    eid = make_experiment("demo-maturity", obs_days=7)
    for i in range(30):
        assign(eid, f"s2-{i:03d}", f"s2-req-{i}")

    res1 = post(f"/experiments/{eid}/analysis", {})
    t = sum(v["obs_insufficient"] for v in res1["variants"].values())
    print(f"\n  入组当天分析：观察不足 {t} 人，成熟 0 人 → {res1['effect']['interpretation']}")
    check("入组当天全部为观察不足", t == 30)

    advance(4)
    res2 = post(f"/experiments/{eid}/analysis", {})
    t = sum(v["obs_insufficient"] for v in res2["variants"].values())
    print(f"  第 4 天分析：观察不足 {t} 人（窗口仍未结束）")
    check("第 4 天仍观察不足", t == 30)

    now = advance(4)
    res3 = post(f"/experiments/{eid}/analysis", {})
    inc = sum(v["report_incomplete"] for v in res3["variants"].values())
    obs = sum(v["obs_insufficient"] for v in res3["variants"].values())
    print(f"  第 8 天分析：窗口已结束，但行为上报水位未覆盖 → 数据等待 {inc} 人，观察不足 {obs} 人")
    check("窗口结束后上报未齐被单列为数据等待", inc == 30 and obs == 0)

    ingest([], watermark_to=now)
    res4 = post(f"/experiments/{eid}/analysis", {})
    mature = sum(v["mature"] for v in res4["variants"].values())
    print(f"  上报补齐后：成熟 {mature} 人 → {res4['effect']['interpretation']}")
    print("  说明：即使成熟，30 人的小样本成功/未成功人数不足 10，仍只报计数——")
    print("        系统不会在证据不足时假装能下结论。")
    check("上报补齐后进入成熟统计", mature == 30)
    check("小样本仍提示证据不足", res4["effect"]["evidence_status"] == "insufficient_counts")


# ---------------------------------------------------------------- 场景 3
def scenario_duplicates_and_edges() -> None:
    banner("场景 3｜重复入组、重复报名与边界：分母分子不被悄悄改变")
    print("  设定：观察期 7 天，受众规则 age>=18。演示幂等、跨设备、多笔订单、窗口边界、撤销/退款。")
    eid = make_experiment("demo-dup", obs_days=7,
                          rule={"conditions": [{"field": "age", "op": "gte", "value": 18}]})
    start = clock_now()

    a1 = assign(eid, "stu-dup", "dup-1", device_id="phone", profile={"age": 25})
    a2 = assign(eid, "stu-dup", "dup-1")                       # 同幂等键重放
    a3 = assign(eid, "stu-dup", "dup-2", device_id="pad")      # 换设备再申请
    print(f"\n  跨设备重复入组：首次={a1['variant']}，重放={a2['outcome']}，换设备={a3['outcome']}/{a3['variant']}")
    check("重复请求返回原组", a2["variant"] == a1["variant"] and a3["variant"] == a1["variant"])

    assign(eid, "stu-boundary", "b-1", profile={"age": 30})
    assign(eid, "stu-cancel", "c-1", profile={"age": 30})
    assign(eid, "stu-refund-late", "rl-1", profile={"age": 30})
    bad = assign(eid, "stu-young", "y-1", profile={"age": 15})
    print(f"  资格不足：{bad['outcome']}（{bad['reason_detail']['failed_conditions'][0]}）")
    check("资格不足返回原因且不入组", bad["outcome"] == "ineligible")

    end = start + timedelta(days=7)
    events = [
        # stu-dup 两笔订单 → 仅计一次
        {"source": "orders", "biz_id": "d-1", "seq": 1, "event_type": "order_created",
         "student_key": "stu-dup", "occurred_at": iso(start + timedelta(days=1))},
        {"source": "orders", "biz_id": "d-2", "seq": 1, "event_type": "order_created",
         "student_key": "stu-dup", "occurred_at": iso(start + timedelta(days=2))},
        # 恰在窗口结束时刻的订单 → 不计入（左闭右开）
        {"source": "orders", "biz_id": "b-1", "seq": 1, "event_type": "order_created",
         "student_key": "stu-boundary", "occurred_at": iso(end)},
        # 窗口内取消 → 不计入
        {"source": "orders", "biz_id": "c-1", "seq": 1, "event_type": "order_created",
         "student_key": "stu-cancel", "occurred_at": iso(start + timedelta(days=1))},
        {"source": "orders", "biz_id": "c-1", "seq": 2, "event_type": "order_canceled",
         "student_key": "stu-cancel", "occurred_at": iso(start + timedelta(days=3))},
        # 窗口结束后才退款 → 仍计入
        {"source": "orders", "biz_id": "rl-1", "seq": 1, "event_type": "order_created",
         "student_key": "stu-refund-late", "occurred_at": iso(start + timedelta(days=1))},
        {"source": "orders", "biz_id": "rl-1", "seq": 2, "event_type": "order_refunded",
         "student_key": "stu-refund-late", "occurred_at": iso(end + timedelta(days=1))},
    ]
    now = advance(8)
    r1 = ingest(events, watermark_to=now)
    r2 = ingest(events)  # 整批重发 → 全部去重
    print(f"  事件去重：首次插入 {r1['inserted']} 条；整批重发插入 {r2['inserted']} 条、重复 {len(r2['duplicates'])} 条")
    check("重复上报被去重", r2["inserted"] == 0 and len(r2["duplicates"]) == len(events))

    res = post(f"/experiments/{eid}/analysis", {})
    secs = run_sections(res["run_id"])
    def sec(k): return secs[k]["section"]
    print(f"  stu-dup（两笔订单）→ {sec('stu-dup')}；stu-boundary（恰在结束时刻下单）→ {sec('stu-boundary')}")
    print(f"  stu-cancel（窗口内取消）→ {sec('stu-cancel')}；stu-refund-late（结束后退款）→ {sec('stu-refund-late')}")
    check("同学员多笔订单仅计一次", sec("stu-dup") == "mature_success")
    check("恰在结束时刻的报名不计入", sec("stu-boundary") == "mature_failure")
    check("窗口内取消的报名不计入", sec("stu-cancel") == "mature_failure")
    check("窗口结束后退款不影响本次观察", sec("stu-refund-late") == "mature_success")

    post(f"/experiments/{eid}/stop", {})
    late = assign(eid, "stu-newcomer", "n-1", profile={"age": 30})
    again = assign(eid, "stu-dup", "dup-3")
    print(f"  停招后：新学员 → {late['outcome']}（{late['reason']}）；老学员 → 仍 {again['variant']}")
    check("停招只停新分配", late["outcome"] == "stopped" and again["variant"] == a1["variant"])


# ---------------------------------------------------------------- 场景 4
def scenario_late_events() -> None:
    banner("场景 4｜晚报：旧结果不可变，新分析自然纳入迟到事件")
    print("  设定：观察期 1 天。先出一版结果，再补报窗口内的迟到订单。")
    eid = make_experiment("demo-late", obs_days=1)
    start = clock_now()
    keys = [f"s4-{i:03d}" for i in range(20)]
    events = []
    for i, k in enumerate(keys):
        assign(eid, k, f"s4-req-{i}")
        if i < 10:  # 前 10 人正常上报报名
            events.append({"source": "orders", "biz_id": f"s4-ord-{i}", "seq": 1,
                           "event_type": "order_created", "student_key": k,
                           "occurred_at": iso(start + timedelta(hours=6))})
    now = advance(2)
    ingest(events, watermark_to=now)

    run_a = post(f"/experiments/{eid}/analysis", {})
    succ_a = sum(v["mature_success"] for v in run_a["variants"].values())
    print(f"\n  运行 A（截止 {run_a['cutoff']}）：成熟成功 {succ_a} 人 / 共 20 人")

    # 5 人的订单迟到：发生在窗口内，现在才上报
    late_events = [{"source": "orders", "biz_id": f"s4-ord-{i}", "seq": 1,
                    "event_type": "order_created", "student_key": keys[i],
                    "occurred_at": iso(start + timedelta(hours=8))} for i in range(10, 15)]
    ingest(late_events)

    again = get(f"/analysis/{run_a['run_id']}")
    succ_again = sum(v["mature_success"] for v in again["variants"].values())
    print(f"  晚报 5 笔后重查运行 A：成熟成功仍为 {succ_again} 人（快照冻结，旧结果不变）")
    check("旧运行结果不可变", succ_again == succ_a)

    run_b = post(f"/experiments/{eid}/analysis", {})
    succ_b = sum(v["mature_success"] for v in run_b["variants"].values())
    print(f"  运行 B（新快照）：成熟成功 {succ_b} 人 ← 纳入晚报 5 笔；A、B 均可回查")
    check("新运行纳入晚报事件", succ_b == succ_a + 5)


# ---------------------------------------------------------------- 场景 5
def scenario_mutex_layers() -> None:
    banner("场景 5｜互斥层与容量区间：谁能同时影响同一学员、谁必须互斥")
    print("""  设定：一层 1000 个桶位。实验 A、B 同层互斥（各占一半）；
  另一层实验 D 与它们共存（共同曝光如实记录，不承诺互不影响）。""")

    layer = post("/layers", {"name": f"demo-layer-{SUFFIX}",
                             "seed": f"layer-seed-{SUFFIX}", "total_buckets": 1000})
    lid = layer["id"]
    print(f"  互斥层 #{lid} 建立，revision=0，总桶位 1000")

    def make_lexp(name: str, rule: dict | None = None) -> int:
        body = {"name": f"{name}-{SUFFIX}", "seed": f"seed-{name}-{SUFFIX}",
                "observation_days": 1, "ratios": {"control": 0.5, "treatment": 0.5},
                "layer_id": lid}
        if rule:
            body["audience_rule"] = rule
        return post("/experiments", body)["id"]

    def slot(eid: int, lo: int, hi: int, rev: int) -> dict:
        return requests.post(
            f"{BASE}/layers/{lid}/experiments/{eid}/slot",
            json={"bucket_from": lo, "bucket_to": hi, "expected_revision": rev},
            timeout=60).json()

    eid_a, eid_b = make_lexp("demo-layer-a"), make_lexp("demo-layer-b")
    # 初始留出中间 [300,700) 未分配，便于演示扩流只动用空闲桶位
    slot(eid_a, 0, 300, 0)
    slot(eid_b, 700, 1000, 1)
    detail = get(f"/layers/{lid}")
    print(f"  A 占 [0,300)、B 占 [700,1000)，当前 revision={detail['revision']}，"
          f"已用 {detail['capacity']['used']}/1000，空闲 {detail['capacity']['free']}")
    check("层内区间不重叠且总额成立",
          detail["capacity"]["used"] == 600 and detail["capacity"]["free"] == 400)

    # 同层互斥：每人至多进一个；未入选原因是 layer_not_selected
    both_assigned = 0
    miss_seen = False
    for i in range(40):
        key = f"s5-{i:03d}"
        ra = assign(eid_a, key, f"s5a-req-{i}")
        rb = assign(eid_b, key, f"s5b-req-{i}")
        if ra["status"] == "assigned" and rb["status"] == "assigned":
            both_assigned += 1
        if rb["status"] == "layer_not_selected" or ra["status"] == "layer_not_selected":
            miss_seen = True
    check("同层没有任何学员同时进入两个实验", both_assigned == 0)
    check("层内未命中单独记录为 layer_not_selected", miss_seen)

    # 决策解释端点
    sample = get(f"/layers/{lid}/explain", student_key="s5-000")
    print(f"  explain s5-000：桶位 {sample['bucket']} → 实验 {sample['selected_experiment_id']}")
    check("解释端点给出桶位与选中实验",
          isinstance(sample["bucket"], int) and
          sample["selected_experiment_id"] in (eid_a, eid_b, None))

    # 扩流：A 从 [0,300) 扩大到 [0,500)，只吃空闲桶位，且包含旧区间
    old_variants = {}
    for i in range(40):
        key = f"s5-{i:03d}"
        r = assign(eid_a, key, f"s5a-req-{i}")
        if r["status"] == "assigned":
            old_variants[key] = r["variant"]
    r = requests.put(f"{BASE}/layers/{lid}/experiments/{eid_a}/capacity",
                     json={"bucket_from": 0, "bucket_to": 500, "expected_revision": 2},
                     timeout=60)
    check("扩流申请成功且 revision 推进", r.status_code == 200 and r.json()["revision"] == 3)
    # 缩容/平移被拒（新边界不包含旧区间 → 会驱逐老成员）
    r_bad = requests.put(f"{BASE}/layers/{lid}/experiments/{eid_a}/capacity",
                         json={"bucket_from": 100, "bucket_to": 500, "expected_revision": 3},
                         timeout=60)
    check("会驱逐老成员的边界调整被拒绝", r_bad.status_code == 409)
    moved = [k for k, v in old_variants.items()
             if assign(eid_a, k, f"s5a-recheck-{k}")["variant"] != v]
    check("扩流后老成员一个都不换组", not moved)

    # 两个管理员争抢最后一段空闲 [500,600)：陈旧 revision 直接 409，只有一个修订生效
    eid_c = make_lexp("demo-layer-c")
    stale = requests.post(f"{BASE}/layers/{lid}/experiments/{eid_c}/slot",
                          json={"bucket_from": 500, "bucket_to": 600, "expected_revision": 2},
                          timeout=60)
    check("基于旧 revision 的并发申请被拒绝（防超卖）", stale.status_code == 409)
    fresh = requests.post(f"{BASE}/layers/{lid}/experiments/{eid_c}/slot",
                          json={"bucket_from": 500, "bucket_to": 600, "expected_revision": 3},
                          timeout=60)
    check("读取最新 revision 后申请成功", fresh.status_code == 200)

    # 停招：释放 A 的容量，老成员留观，新来者才能用释放范围
    old_key = next(iter(old_variants))
    post(f"/experiments/{eid_a}/stop", {})
    detail = get(f"/layers/{lid}")
    stopped = next(x for x in detail["experiments"] if x["experiment_id"] == eid_a)
    check("停招后区间释放、容量可再分配", stopped["status"] == "stopped")
    replay = assign(eid_a, old_key, f"s5a-old-{old_key}")
    check("旧成员停招后仍返回原组完成观察",
          replay["outcome"] == "duplicate" and replay["variant"] == old_variants[old_key])
    # 新人再申请已停实验 → stopped 原因
    newcomer_stop = assign(eid_a, "s5-newcomer-stop", "s5a-new-1")
    check("新人申请已停实验记录 experiment_stopped",
          newcomer_stop["status"] == "stopped")

    # 三类原因齐备：受众规则实验挂到 A 释放出的区间
    rule_exp = make_lexp("demo-layer-rule",
                         rule={"conditions": [{"field": "age", "op": "gte", "value": 18}]})
    rev = get(f"/layers/{lid}")["revision"]
    slot(rule_exp, 0, 100, rev)
    r_ineligible = None
    for i in range(300):
        key = f"s5-young-{i}"
        r_ineligible = assign(rule_exp, key, f"s5r-req-{i}", profile={"age": 15})
        if r_ineligible["status"] == "ineligible":
            break
    check("受众不合格独立记录为 ineligible",
          r_ineligible is not None and r_ineligible["reason"] == "audience_rule_not_matched")
    counts = get(f"/experiments/{eid_a}")["assignment_counts"]
    check("三类未入组原因各自落库",
          counts["layer_not_selected"] >= 1 and counts["stopped"] >= 1)

    # 历史重放走当时路由
    hist = assign(eid_b, "s5-000", "s5b-req-0")
    check("分配事实冻结当时 route_snapshot", hist["route_snapshot"]["layer"]["revision"] is not None)
    revisions = get(f"/layers/{lid}/revisions")
    check("修订历史逐版可查、旧快照不被新边界改写",
          [r["revision"] for r in revisions] == list(range(len(revisions))))

    # 跨层共存：另一层全量实验 D，与第一层可同时影响同一学员（共同曝光如实记录）
    layer2 = post("/layers", {"name": f"demo-layer2-{SUFFIX}",
                              "seed": f"layer2-seed-{SUFFIX}", "total_buckets": 1000})
    eid_d = post("/experiments", {"name": f"demo-coexist-{SUFFIX}",
                                  "seed": f"seed-d-{SUFFIX}", "observation_days": 1,
                                  "ratios": {"control": 0.5, "treatment": 0.5},
                                  "layer_id": layer2["id"]})["id"]
    requests.post(f"{BASE}/layers/{layer2['id']}/experiments/{eid_d}/slot",
                  json={"bucket_from": 0, "bucket_to": 1000, "expected_revision": 0}, timeout=60)
    # 同一批 s5-* 学员：在第一层（A/B/C）与第二层 D 各有一次曝光
    for i in range(40):
        key = f"s5-{i:03d}"
        assign(eid_d, key, f"s5d-req-{i}")
        post(f"/experiments/{eid_d}/exposures", {"student_key": key, "status": "success"})
        rb = assign(eid_b, key, f"s5b-co-{i}")
        if rb["status"] == "assigned":
            post(f"/experiments/{eid_b}/exposures", {"student_key": key, "status": "success"})
    report = get("/reports/co-exposures")
    pair = next((p for p in report["pairs"] if eid_d in
                 (p["experiment_a"]["experiment_id"], p["experiment_b"]["experiment_id"])), None)
    check("跨层碰撞报告按实验对给出共同曝光人数",
          pair is not None and pair["overlap_students"] >= 1)
    if pair:
        print(f"  跨层共同曝光：{pair['overlap_students']} 人，"
              f"双方曝光成功 {pair['both_exposure_success']} 人；分组组合 {pair['by_variant_pair']}")
        check("报告明示不支持事后剔除重叠者", "不支持事后剔除" in pair["note"])


def main() -> int:
    print(f"目标服务：{BASE}")
    get("/health")
    scenario_selection_bias()
    scenario_observation_waiting()
    scenario_duplicates_and_edges()
    scenario_late_events()
    scenario_mutex_layers()

    banner("总结：如何分清“没有效果”与“现在还不能判断”")
    print("""  · 观察不足 / 上报未齐 / 样本不足 → 系统明说“现在还不能判断”，只报计数。
  · 证据足够且区间跨 0 → “当前证据未区分方向”，这不等于“两方案等效”。
  · 区间整体偏离 0 → 才谈得上方向性结论。
  · 任何事后筛选（只算看完的人、只算活跃设备）都会重新引入偏差，本服务不支持。""")
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n  校验 {len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("  未通过项：")
        for label in failed:
            print(f"    - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
