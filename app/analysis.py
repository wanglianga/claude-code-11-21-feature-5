"""分析引擎。

原则：
- 意向分析（ITT）：首次分配的全部合格对象都计入，未观看者也在内，不得事后筛除。
- 统一截止点：观察窗口 [入组时刻, 入组时刻+观察天数)，UTC、左闭右开；
  恰在结束时刻的报名不计入。窗口未结束 → 观察不足；窗口已结束但上报水位未覆盖 → 数据等待。
- 有效报名：窗口内创建、且窗口结束前未被有效撤销的至少一笔订单；同学员多笔仅计一次。
- 每次运行冻结 截止点/事件序号/水位线/比例配置 快照；晚报事件只属于更新的运行，旧结果不可变。
- 卡方检查按各比例配置生效期间的实际入组人数分别计算期望，再合并偏差。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import clock, layers
from .events import effective_watermark, max_event_seq
from .models import (
    VARIANTS,
    AnalysisResult,
    AnalysisRun,
    AnalysisStudent,
    Assignment,
    Event,
    Experiment,
)
from .services import DomainError, get_experiment, list_ratio_configs
from .stats import chi2_sf, diff_ci

SECTION_OBS_INSUFFICIENT = "obs_insufficient"      # 观察不足：窗口尚未结束
SECTION_REPORT_INCOMPLETE = "report_incomplete"    # 上报未齐：窗口已结束但水位未覆盖
SECTION_MATURE_SUCCESS = "mature_success"          # 成熟且有效报名
SECTION_MATURE_FAILURE = "mature_failure"          # 成熟且未有效报名
SECTIONS = (
    SECTION_OBS_INSUFFICIENT,
    SECTION_REPORT_INCOMPLETE,
    SECTION_MATURE_SUCCESS,
    SECTION_MATURE_FAILURE,
)

MIN_CELL = 10  # 两组成功/未成功均不少于 10 才给正态近似区间


# ------------------------------------------------------------ 单学员评估

def evaluate_enrollment(
    events: list[Event],
    window_start: datetime,
    window_end: datetime,
    created_types: set[str],
    revoke_types: set[str],
) -> tuple[bool, Event | None, list[int]]:
    """在已按快照过滤的事件集合上判定有效报名。

    返回 (是否有效报名, 有效订单事件, 依据事件 id 列表)。
    撤销匹配同一 (source, biz_id)，且发生在 [订单时刻, 窗口结束) 内。
    """
    orders = [
        e
        for e in events
        if e.event_type in created_types and window_start <= e.occurred_at < window_end
    ]
    orders.sort(key=lambda e: (e.occurred_at, e.id))
    for order in orders:
        revoked = any(
            r.event_type in revoke_types
            and r.source == order.source
            and r.biz_id == order.biz_id
            and order.occurred_at <= r.occurred_at < window_end
            for r in events
        )
        if not revoked:
            return True, order, [order.id]
    return False, None, []


def classify_student(
    assigned_at: datetime,
    observation_days: int,
    cutoff: datetime,
    watermark: datetime | None,
) -> tuple[datetime, datetime, str | None]:
    """返回 (window_start, window_end, 未成熟分段或 None=成熟)。"""
    ws = assigned_at
    we = ws + timedelta(days=observation_days)
    if we > cutoff:
        return ws, we, SECTION_OBS_INSUFFICIENT
    if watermark is None or we > watermark:
        return ws, we, SECTION_REPORT_INCOMPLETE
    return ws, we, None


# ------------------------------------------------------------ 运行创建/续算

def create_run(session: Session, experiment_id: int) -> AnalysisRun:
    """以当前时钟为截止点创建新运行（status=running），随后须 compute_run 完成。"""
    exp = get_experiment(session, experiment_id)
    now = clock.now(session)
    configs = list_ratio_configs(session, exp.id)
    layer_snapshot = None
    if exp.layer_id is not None:
        layer = layers.get_layer(session, exp.layer_id)
        # 预先声明的分层结果：运行一旦创建即冻结，分析者不能看完效果再剔除重叠者
        layer_snapshot = {
            "layer": {
                "id": layer.id,
                "name": layer.name,
                "seed": layer.seed,
                "total_buckets": layer.total_buckets,
                "revision": layer.revision,
            },
            "routing": layers.route_snapshot(session, layer),
            "declared_note": (
                "分层/共存关系为运行创建时预先声明的事实；分析按 ITT 全量进行，"
                "不提供事后剔除跨层重叠学员的入口"
            ),
        }
    run = AnalysisRun(
        experiment_id=exp.id,
        cutoff=now,
        event_seq_snapshot=max_event_seq(session),
        watermark_snapshot=effective_watermark(session),
        ratio_snapshot=[
            {"ratio_config_id": c.id, "effective_from": c.effective_from.isoformat(), "ratios": c.ratios}
            for c in configs
        ],
        layer_snapshot=layer_snapshot,
        status="running",
        created_at=now,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def resume_run(session: Session, run_id: int) -> AnalysisRun:
    """断点续算：按运行创建时冻结的截止点与快照重新计算同一批次。"""
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise DomainError(f"分析运行不存在: {run_id}", 404)
    if run.status == "done":
        return run
    compute_run(session, run)
    return run


def compute_run(session: Session, run: AnalysisRun) -> AnalysisRun:
    """执行计算并落库；重入安全（先清除本运行的半成品）。"""
    exp = get_experiment(session, run.experiment_id)
    cutoff = run.cutoff
    watermark = run.watermark_snapshot
    seq_limit = run.event_seq_snapshot

    # 清除半成品（续算场景）
    session.execute(delete(AnalysisStudent).where(AnalysisStudent.run_id == run.id))
    session.execute(delete(AnalysisResult).where(AnalysisResult.run_id == run.id))
    session.flush()

    # 首次分配的全部合格对象（ITT：含未曝光/曝光失败者）
    assignments = list(
        session.scalars(
            select(Assignment)
            .where(Assignment.experiment_id == exp.id, Assignment.status == "assigned")
            .order_by(Assignment.id)
        )
    )
    student_keys = [a.student_key for a in assignments]

    # 快照内事件（晚报事件 id 更大，天然排除）
    events_by_student: dict[str, list[Event]] = {k: [] for k in student_keys}
    if student_keys:
        for e in session.scalars(
            select(Event)
            .where(Event.id <= seq_limit, Event.student_key.in_(student_keys))
            .order_by(Event.id)
        ):
            events_by_student.setdefault(e.student_key, []).append(e)

    created_types = set(exp.enrollment_definition["created_events"])
    revoke_types = set(exp.enrollment_definition["revoke_events"])

    counts = {
        v: dict(total=0, obs=0, inc=0, mature=0, succ=0, fail=0) for v in VARIANTS
    }
    student_rows: list[AnalysisStudent] = []
    for a in assignments:
        c = counts[a.variant]
        c["total"] += 1
        ws, we, pending = classify_student(a.assigned_at, exp.observation_days, cutoff, watermark)
        section = pending
        qual_source = qual_biz = None
        basis: list[int] = []
        if section is None:
            ok, order, basis = evaluate_enrollment(
                events_by_student.get(a.student_key, []), ws, we, created_types, revoke_types
            )
            section = SECTION_MATURE_SUCCESS if ok else SECTION_MATURE_FAILURE
            if ok:
                qual_source, qual_biz = order.source, order.biz_id
        if section == SECTION_OBS_INSUFFICIENT:
            c["obs"] += 1
        elif section == SECTION_REPORT_INCOMPLETE:
            c["inc"] += 1
        elif section == SECTION_MATURE_SUCCESS:
            c["mature"] += 1
            c["succ"] += 1
        else:
            c["mature"] += 1
            c["fail"] += 1
        student_rows.append(
            AnalysisStudent(
                run_id=run.id,
                student_key=a.student_key,
                variant=a.variant,
                section=section,
                window_start=ws,
                window_end=we,
                exposure_status=a.exposure_status,
                qualifying_source=qual_source,
                qualifying_biz_id=qual_biz,
                basis_event_ids=basis or None,
            )
        )

    for row in student_rows:
        session.add(row)
    for v in VARIANTS:
        c = counts[v]
        session.add(
            AnalysisResult(
                run_id=run.id,
                variant=v,
                total_assigned=c["total"],
                obs_insufficient=c["obs"],
                report_incomplete=c["inc"],
                mature=c["mature"],
                mature_success=c["succ"],
                mature_failure=c["fail"],
            )
        )

    # 对账：分段合计必须等于分配事实总数
    reconciled = all(
        c["total"] == c["obs"] + c["inc"] + c["mature"]
        and c["mature"] == c["succ"] + c["fail"]
        for c in counts.values()
    ) and len(student_rows) == sum(c["total"] for c in counts.values())

    _compute_chi2(session, run, exp, assignments)
    _compute_effect(run, exp, counts)

    run.reconciled = reconciled
    run.status = "done"
    run.completed_at = clock.now(session)
    session.commit()
    session.refresh(run)
    return run


def _compute_chi2(session: Session, run: AnalysisRun, exp: Experiment, assignments: list[Assignment]) -> None:
    """按各比例配置生效期间的实际合格入组人数分别算期望，再合并偏差。"""
    configs = list_ratio_configs(session, exp.id)
    observed: dict[int, dict[str, int]] = {}
    for a in assignments:
        bucket = observed.setdefault(a.ratio_config_id, {v: 0 for v in VARIANTS})
        bucket[a.variant] += 1

    stat = 0.0
    df = 0
    details: list[dict] = []
    for cfg in configs:
        obs = observed.get(cfg.id, {v: 0 for v in VARIANTS})
        n = sum(obs.values())
        if n == 0:
            continue
        expected = {v: n * cfg.ratios[v] for v in VARIANTS}
        used = 0
        for v in VARIANTS:
            e = expected[v]
            if e <= 0:
                continue
            used += 1
            contrib = (obs[v] - e) ** 2 / e
            stat += contrib
            details.append(
                {
                    "ratio_config_id": cfg.id,
                    "effective_from": cfg.effective_from.isoformat(),
                    "variant": v,
                    "observed": obs[v],
                    "expected": round(e, 4),
                    "configured_ratio": cfg.ratios[v],
                    "contribution": round(contrib, 6),
                }
            )
        df += max(0, used - 1)

    if df >= 1:
        p = chi2_sf(stat, df)
        run.chi2_stat = round(stat, 6)
        run.chi2_df = df
        run.chi2_p = p
        run.chi2_paused = bool(p < exp.chi2_threshold)
    else:
        run.chi2_stat = None
        run.chi2_df = 0
        run.chi2_p = None
        run.chi2_paused = False
    run.chi2_details = sorted(details, key=lambda d: -d["contribution"])


def _compute_effect(run: AnalysisRun, exp: Experiment, counts: dict) -> None:
    ct, cc = counts["treatment"], counts["control"]
    reasons: list[str] = []

    if run.chi2_paused:
        run.evidence_status = "chi2_paused"
        run.evidence_reasons = [
            f"分流人数与预设比例显著偏离（卡方 p={run.chi2_p:.6g} < 阈值 {exp.chi2_threshold:g}），"
            "暂停效果判断；异常来源见 chi2_details"
        ]
        run.interpretation = "分流异常，暂停效果判断：请先排查分流链路，再解读效果"
        run.diff = None
        run.ci_low = run.ci_high = run.ci_method = None
        return

    n_t, n_c = ct["mature"], cc["mature"]
    if n_t == 0 or n_c == 0:
        run.diff = None
    else:
        run.diff = ct["succ"] / n_t - cc["succ"] / n_c

    cells = {
        "treatment.mature_success": ct["succ"],
        "treatment.mature_failure": ct["fail"],
        "control.mature_success": cc["succ"],
        "control.mature_failure": cc["fail"],
    }
    short = {k: v for k, v in cells.items() if v < MIN_CELL}
    if short:
        run.evidence_status = "insufficient_counts"
        for k, v in short.items():
            reasons.append(f"{k} = {v} < {MIN_CELL}")
        run.evidence_reasons = [
            "成熟样本的成功/未成功人数不足，只报告计数与差值，不给出区间：",
            *reasons,
        ]
        run.interpretation = "证据不足，现在还不能判断（既非“没有效果”，也非“两方案等效”）"
        run.ci_low = run.ci_high = run.ci_method = None
        return

    p_t, p_c = ct["succ"] / n_t, cc["succ"] / n_c
    diff, low, high = diff_ci(p_t, n_t, p_c, n_c)
    run.diff = diff
    run.ci_low, run.ci_high = low, high
    run.ci_method = "normal_approximation_95"
    run.evidence_status = "ok"
    run.evidence_reasons = []
    if low > 0:
        run.interpretation = "95%区间整体高于 0：当前证据偏向新方案报名率更高"
    elif high < 0:
        run.interpretation = "95%区间整体低于 0：当前证据偏向旧方案报名率更高"
    else:
        run.interpretation = "95%区间跨 0：当前证据未区分新旧方案方向（注意：不代表两方案等效）"


# ------------------------------------------------------------ 查询

def run_result(session: Session, run_id: int) -> dict:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise DomainError(f"分析运行不存在: {run_id}", 404)
    results = {
        r.variant: {
            "total_assigned": r.total_assigned,
            "obs_insufficient": r.obs_insufficient,
            "report_incomplete": r.report_incomplete,
            "mature": r.mature,
            "mature_success": r.mature_success,
            "mature_failure": r.mature_failure,
        }
        for r in session.scalars(select(AnalysisResult).where(AnalysisResult.run_id == run.id))
    }
    return {
        "run_id": run.id,
        "experiment_id": run.experiment_id,
        "status": run.status,
        "cutoff": run.cutoff.isoformat(),
        "event_seq_snapshot": run.event_seq_snapshot,
        "watermark_snapshot": run.watermark_snapshot.isoformat() if run.watermark_snapshot else None,
        "ratio_snapshot": run.ratio_snapshot,
        "layer_snapshot": run.layer_snapshot,
        "variants": results,
        "effect": {
            "diff": run.diff,
            "ci_low": run.ci_low,
            "ci_high": run.ci_high,
            "ci_method": run.ci_method,
            "evidence_status": run.evidence_status,
            "evidence_reasons": run.evidence_reasons,
            "interpretation": run.interpretation,
        },
        "chi2": {
            "stat": run.chi2_stat,
            "df": run.chi2_df,
            "p_value": run.chi2_p,
            "paused": run.chi2_paused,
            "details": run.chi2_details,
        },
        "reconciled": run.reconciled,
        "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def run_students(
    session: Session,
    run_id: int,
    *,
    section: str | None = None,
    variant: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> dict:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise DomainError(f"分析运行不存在: {run_id}", 404)
    q = select(AnalysisStudent).where(AnalysisStudent.run_id == run_id)
    if section:
        q = q.where(AnalysisStudent.section == section)
    if variant:
        q = q.where(AnalysisStudent.variant == variant)
    total = session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = session.scalars(q.order_by(AnalysisStudent.student_key).limit(limit).offset(offset))
    return {
        "run_id": run_id,
        "total": total,
        "students": [
            {
                "student_key": r.student_key,
                "variant": r.variant,
                "section": r.section,
                "window_start": r.window_start.isoformat(),
                "window_end": r.window_end.isoformat(),
                "exposure_status": r.exposure_status,
                "qualifying_source": r.qualifying_source,
                "qualifying_biz_id": r.qualifying_biz_id,
                "basis_event_ids": r.basis_event_ids,
            }
            for r in rows
        ],
    }


def student_view(session: Session, experiment_id: int, student_key: str) -> dict:
    """按学员回查：资格判定、分组、成熟条件与报名依据。"""
    exp = get_experiment(session, experiment_id)
    a = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id, Assignment.student_key == student_key
        )
    ).first()
    if a is None:
        raise DomainError("该学员在此实验中没有任何分配记录", 404)

    view: dict = {
        "experiment_id": exp.id,
        "student_key": student_key,
        "assignment": {
            "status": a.status,
            "variant": a.variant,
            "reason": a.reason,
            "reason_detail": a.reason_detail,
            "assigned_at": a.assigned_at.isoformat(),
            "ratio_config_id": a.ratio_config_id,
            "exposure_status": a.exposure_status,
            "eligibility": a.eligibility,
            "layer_id": a.layer_id,
            "layer_bucket": a.layer_bucket,
            "route_revision": a.route_revision,
            "route_snapshot": a.route_snapshot,
        },
        "maturity_now": None,
        "enrollment_basis_now": None,
        "co_exposures": layers.student_co_exposures(session, student_key) if a.layer_id else None,
        "runs": [],
    }

    if a.status == "assigned":
        now = clock.now(session)
        watermark = effective_watermark(session)
        ws, we, pending = classify_student(a.assigned_at, exp.observation_days, now, watermark)
        view["maturity_now"] = {
            "window_start": ws.isoformat(),
            "window_end": we.isoformat(),
            "observation_days": exp.observation_days,
            "cutoff_now": now.isoformat(),
            "watermark_now": watermark.isoformat() if watermark else None,
            "section": pending or "mature",
        }
        if pending is None:
            events = list(
                session.scalars(
                    select(Event)
                    .where(Event.id <= max_event_seq(session), Event.student_key == student_key)
                    .order_by(Event.id)
                )
            )
            ok, order, basis = evaluate_enrollment(
                events,
                ws,
                we,
                set(exp.enrollment_definition["created_events"]),
                set(exp.enrollment_definition["revoke_events"]),
            )
            basis_events = [e for e in events if e.id in basis] if basis else []
            view["enrollment_basis_now"] = {
                "enrolled": ok,
                "qualifying_order": (
                    {"source": order.source, "biz_id": order.biz_id, "occurred_at": order.occurred_at.isoformat()}
                    if order else None
                ),
                "basis_events": [
                    {
                        "id": e.id,
                        "source": e.source,
                        "biz_id": e.biz_id,
                        "seq": e.seq,
                        "event_type": e.event_type,
                        "occurred_at": e.occurred_at.isoformat(),
                    }
                    for e in basis_events
                ],
            }

    runs = session.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.experiment_id == exp.id, AnalysisRun.status == "done")
        .order_by(AnalysisRun.id)
    )
    for r in runs:
        row = session.get(AnalysisStudent, (r.id, student_key))
        if row is not None:
            view["runs"].append(
                {
                    "run_id": r.id,
                    "cutoff": r.cutoff.isoformat(),
                    "section": row.section,
                    "qualifying_source": row.qualifying_source,
                    "qualifying_biz_id": row.qualifying_biz_id,
                    "basis_event_ids": row.basis_event_ids,
                }
            )
    return view
