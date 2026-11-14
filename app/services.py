"""实验、学员与分配的核心业务逻辑。

关键约束：
- 分配先于方案展示；曝光失败仍保留分配事实。
- 首次申请时按当时资料判定资格并快照；重复请求（同学员或同 request_id）返回首次事实。
- 换设备不换组：按稳定 student_key 哈希分流，设备仅做绑定解析。
- 实验停招只停新分配，既有分配与观察继续。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock, layers
from .eligibility import evaluate, failed_conditions, validate_rule
from .errors import DomainError  # 再导出：events/analysis 仍可 from .services import DomainError
from .models import VARIANTS, Assignment, Experiment, RatioConfig, Student, StudentDevice

DEFAULT_ENROLLMENT_DEFINITION = {
    "created_events": ["order_created"],
    "revoke_events": ["order_canceled", "order_refunded"],
}


# ---------------------------------------------------------------- 实验

def validate_ratios(ratios: dict) -> dict:
    if not isinstance(ratios, dict) or set(ratios.keys()) != set(VARIANTS):
        raise DomainError(f"ratios 必须且只能包含键：{', '.join(VARIANTS)}")
    out = {}
    for k in VARIANTS:
        v = ratios[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or v > 1:
            raise DomainError(f"ratios.{k} 必须是 [0,1] 内的数值")
        out[k] = float(v)
    if abs(sum(out.values()) - 1.0) > 1e-6:
        raise DomainError("ratios 各组比例之和必须等于 1")
    return out


def create_experiment(
    session: Session,
    *,
    name: str,
    seed: str,
    observation_days: int,
    ratios: dict,
    audience_rule: dict | None = None,
    enrollment_definition: dict | None = None,
    chi2_threshold: float = 0.001,
    layer_id: int | None = None,
) -> Experiment:
    if not name:
        raise DomainError("name 不能为空")
    if not seed:
        raise DomainError("seed 不能为空（固定种子，创建后不可改）")
    if not isinstance(observation_days, int) or observation_days < 1:
        raise DomainError("observation_days 必须为 >=1 的整数")
    if not (0 < chi2_threshold < 1):
        raise DomainError("chi2_threshold 必须在 (0,1) 内")
    if layer_id is not None:
        layers.get_layer(session, layer_id)  # 层必须存在
    rule = audience_rule or {"conditions": []}
    try:
        validate_rule(rule)
    except ValueError as e:
        raise DomainError(str(e)) from e
    definition = enrollment_definition or DEFAULT_ENROLLMENT_DEFINITION
    for key in ("created_events", "revoke_events"):
        if key not in definition or not isinstance(definition[key], list) or not definition[key]:
            raise DomainError(f"enrollment_definition.{key} 必须是非空事件类型数组")
    now = clock.now(session)
    exp = Experiment(
        name=name,
        seed=str(seed),
        status="enrolling",
        audience_rule=rule,
        enrollment_definition=definition,
        observation_days=observation_days,
        chi2_threshold=float(chi2_threshold),
        layer_id=layer_id,
        created_at=now,
    )
    session.add(exp)
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise DomainError(f"实验名称已存在: {name}", 409) from e
    rc = RatioConfig(experiment_id=exp.id, effective_from=now, ratios=validate_ratios(ratios))
    session.add(rc)
    session.commit()
    session.refresh(exp)
    return exp


def get_experiment(session: Session, experiment_id: int) -> Experiment:
    exp = session.get(Experiment, experiment_id)
    if exp is None:
        raise DomainError(f"实验不存在: {experiment_id}", 404)
    return exp


def list_ratio_configs(session: Session, experiment_id: int) -> list[RatioConfig]:
    return list(
        session.scalars(
            select(RatioConfig)
            .where(RatioConfig.experiment_id == experiment_id)
            .order_by(RatioConfig.effective_from, RatioConfig.id)
        )
    )


def active_ratio_config(session: Session, experiment_id: int, at: datetime) -> RatioConfig:
    """取生效时间 <= at 的最新配置。"""
    rc = session.scalars(
        select(RatioConfig)
        .where(RatioConfig.experiment_id == experiment_id, RatioConfig.effective_from <= at)
        .order_by(RatioConfig.effective_from.desc(), RatioConfig.id.desc())
        .limit(1)
    ).first()
    if rc is None:
        raise DomainError("实验缺少生效中的分流比例配置", 500)
    return rc


def change_ratio(session: Session, experiment_id: int, ratios: dict) -> RatioConfig:
    """调整比例：插入新配置，仅作用于生效之后的分配；历史分配仍归属原配置期间。"""
    exp = get_experiment(session, experiment_id)
    if exp.status != "enrolling":
        raise DomainError("实验已停招，不能再调整比例", 409)
    now = clock.now(session)
    rc = RatioConfig(experiment_id=exp.id, effective_from=now, ratios=validate_ratios(ratios))
    session.add(rc)
    session.commit()
    session.refresh(rc)
    return rc


def stop_experiment(session: Session, experiment_id: int) -> Experiment:
    exp = get_experiment(session, experiment_id)
    if exp.status == "stopped":
        return exp
    now = clock.now(session)
    exp.status = "stopped"
    exp.stopped_at = now
    # 分层实验：释放层内容量，供新来者使用；老成员分配事实保留，观察继续
    if exp.layer_id is not None:
        layer = layers.get_layer(session, exp.layer_id)
        layers.release_slot(session, layer, exp.id)
    session.commit()
    session.refresh(exp)
    return exp


# ---------------------------------------------------------------- 学员

def register_student(
    session: Session,
    *,
    student_key: str | None = None,
    device_id: str | None = None,
    profile: dict | None = None,
) -> tuple[Student, bool]:
    """注册或解析学员；返回 (student, created)。device_id 已绑定时解析到原学员。"""
    now = clock.now(session)
    student: Student | None = None
    if student_key:
        student = session.scalars(select(Student).where(Student.student_key == student_key)).first()
    if student is None and device_id:
        bound = session.get(StudentDevice, device_id)
        if bound is not None:
            student = session.scalars(
                select(Student).where(Student.student_key == bound.student_key)
            ).first()
    created = False
    if student is None:
        student = Student(
            student_key=student_key or f"stu_{uuid.uuid4().hex[:12]}",
            profile=profile or {},
            created_at=now,
        )
        session.add(student)
        session.flush()
        created = True
    elif profile:
        student.profile = {**student.profile, **profile}
        session.flush()
    if device_id and session.get(StudentDevice, device_id) is None:
        session.add(StudentDevice(device_id=device_id, student_key=student.student_key, first_seen_at=now))
        session.flush()
    return student, created


# ---------------------------------------------------------------- 分流

def variant_for(seed: str, student_key: str, ratios: dict) -> str:
    """按 (seed, student_key) 稳定哈希到 [0,1) 区间，再按当前比例映射到组。"""
    digest = hashlib.sha256(f"{seed}:{student_key}".encode("utf-8")).hexdigest()
    x = int(digest[:16], 16) / 2**64
    cumulative = 0.0
    for variant in sorted(ratios.keys()):  # control < treatment，顺序固定
        cumulative += ratios[variant]
        if x < cumulative:
            return variant
    return sorted(ratios.keys())[-1]


def _assignment_payload(a: Assignment, *, outcome: str, idempotent_replay: bool = False) -> dict:
    return {
        "outcome": outcome,
        "idempotent_replay": idempotent_replay,
        "experiment_id": a.experiment_id,
        "student_key": a.student_key,
        "request_id": a.request_id,
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
    }


def _layer_route_snapshot(
    session: Session, layer, bucket: int, exp: Experiment, rc: RatioConfig | None
) -> dict:
    """冻结当时路由与实验配置：历史重放只能用这份快照解释旧入组。"""
    return {
        "layer": {
            "id": layer.id,
            "name": layer.name,
            "seed": layer.seed,
            "total_buckets": layer.total_buckets,
            "revision": layer.revision,
        },
        "bucket": bucket,
        "routing": layers.route_snapshot(session, layer),
        "experiment": {
            "id": exp.id,
            "name": exp.name,
            "seed": exp.seed,
            "status": exp.status,
            "audience_rule": exp.audience_rule,
        },
        "ratio_config": (
            {"id": rc.id, "effective_from": rc.effective_from.isoformat(), "ratios": rc.ratios}
            if rc is not None else None
        ),
    }


def request_assignment(
    session: Session,
    experiment_id: int,
    *,
    request_id: str,
    student_key: str | None = None,
    device_id: str | None = None,
    profile: dict | None = None,
) -> dict:
    """申请分配。返回 outcome ∈ assigned | duplicate | ineligible | layer_not_selected | stopped。"""
    if not request_id:
        raise DomainError("request_id 不能为空（幂等键）")
    exp = get_experiment(session, experiment_id)

    # 1) 幂等键重放：同一 request_id 直接返回首次结果（绑定当时路由的原决定）
    existing = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id, Assignment.request_id == request_id
        )
    ).first()
    if existing is not None:
        if student_key and existing.student_key != student_key:
            raise DomainError(
                f"request_id {request_id} 已用于学员 {existing.student_key}，不能复用", 409
            )
        return _assignment_payload(existing, outcome="duplicate", idempotent_replay=True)

    # 2) 解析稳定学员标识（显式 key 优先；否则按设备绑定；都没有则新建）
    student, _ = register_student(
        session, student_key=student_key, device_id=device_id, profile=profile
    )

    # 3) 同学员重复请求（含跨设备）：返回首次事实
    existing = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id, Assignment.student_key == student.student_key
        )
    ).first()
    if existing is not None:
        return _assignment_payload(existing, outcome="duplicate")

    now = clock.now(session)

    # 4) 分层实验：先按稳定标识在层内选实验（桶位），再进入该实验分组
    layer = layers.get_layer(session, exp.layer_id) if exp.layer_id is not None else None
    bucket = None
    hit_le = None
    prior_in_layer = None
    if layer is not None:
        bucket, hit_le, _ = layers.select_layer_experiment(session, layer, student.student_key)
        # 老成员黏性：已在同层其他实验入组（assigned）的学员不是"新来者"，
        # 停招释放出的范围只服务从未在该层入组的学员；旧成员留在原实验完成观察
        prior_in_layer = session.scalars(
            select(Assignment)
            .join(Experiment, Assignment.experiment_id == Experiment.id)
            .where(
                Assignment.student_key == student.student_key,
                Experiment.layer_id == layer.id,
                Assignment.status == "assigned",
                Assignment.experiment_id != exp.id,
            )
            .limit(1)
        ).first()

    # 5) 依次决定首次事实：停招 > 层内未命中 > 资格不足 > 正常分配
    if exp.status != "enrolling":
        rc = active_ratio_config(session, exp.id, now) if layer is not None else None
        row = Assignment(
            experiment_id=exp.id,
            student_key=student.student_key,
            request_id=request_id,
            device_id=device_id,
            status="stopped",
            reason=layers.REASON_STOPPED,
            reason_detail={"message": "实验已停招，不再接纳新分配；既有观察继续"},
            assigned_at=now,
            layer_id=layer.id if layer else None,
            layer_bucket=bucket,
            route_revision=layer.revision if layer else None,
            route_snapshot=(
                _layer_route_snapshot(session, layer, bucket, exp, rc) if layer else None
            ),
        )
        outcome = "stopped"
    elif layer is not None and (
        hit_le is None or hit_le.experiment_id != exp.id or prior_in_layer is not None
    ):
        # 层内未命中：桶位未分配给本实验（无实验区间/属同层其他实验），
        # 或该学员已是同层其他实验的老成员（释放范围仅新来者可用）
        owner = hit_le.experiment_id if hit_le is not None else None
        sticky_note = (
            f"学员已在同层实验 {prior_in_layer.experiment_id} 入组，"
            "释放/新增容量只对新来者开放，旧成员留在原实验完成观察"
            if prior_in_layer is not None else None
        )
        row = Assignment(
            experiment_id=exp.id,
            student_key=student.student_key,
            request_id=request_id,
            device_id=device_id,
            status=layers.STATUS_LAYER_MISS,
            reason=layers.REASON_LAYER_MISS,
            reason_detail={
                "message": "学员桶位未命中本实验的在招容量区间（层内未选中本实验）",
                "bucket": bucket,
                "total_buckets": layer.total_buckets,
                "owner_experiment_id": owner,
                "sticky_member_of_experiment": (
                    prior_in_layer.experiment_id if prior_in_layer is not None else None
                ),
                "sticky_note": sticky_note,
                "route_revision": layer.revision,
            },
            assigned_at=now,
            layer_id=layer.id,
            layer_bucket=bucket,
            route_revision=layer.revision,
            route_snapshot=_layer_route_snapshot(session, layer, bucket, exp, None),
        )
        outcome = layers.STATUS_LAYER_MISS
    else:
        # 资格：首次申请时按当时资料判定并快照
        evaluation = evaluate(exp.audience_rule, student.profile)
        if not evaluation["eligible"]:
            rc = active_ratio_config(session, exp.id, now) if layer is not None else None
            row = Assignment(
                experiment_id=exp.id,
                student_key=student.student_key,
                request_id=request_id,
                device_id=device_id,
                status="ineligible",
                reason=layers.REASON_AUDIENCE,
                reason_detail={"failed_conditions": failed_conditions(evaluation)},
                eligibility=evaluation,
                assigned_at=now,
                layer_id=layer.id if layer else None,
                layer_bucket=bucket,
                route_revision=layer.revision if layer else None,
                route_snapshot=(
                    _layer_route_snapshot(session, layer, bucket, exp, rc) if layer else None
                ),
            )
            outcome = "ineligible"
        else:
            rc = active_ratio_config(session, exp.id, now)
            row = Assignment(
                experiment_id=exp.id,
                student_key=student.student_key,
                request_id=request_id,
                device_id=device_id,
                status="assigned",
                variant=variant_for(exp.seed, student.student_key, rc.ratios),
                eligibility=evaluation,
                ratio_config_id=rc.id,
                assigned_at=now,
                layer_id=layer.id if layer else None,
                layer_bucket=bucket,
                route_revision=layer.revision if layer else None,
                route_snapshot=(
                    _layer_route_snapshot(session, layer, bucket, exp, rc) if layer else None
                ),
            )
            outcome = "assigned"

    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # 并发下另一请求已写入同学员/同键记录：回滚后返回首次事实
        session.rollback()
        existing = session.scalars(
            select(Assignment).where(
                Assignment.experiment_id == exp.id,
                Assignment.student_key == student.student_key,
            )
        ).first()
        if existing is not None:
            return _assignment_payload(existing, outcome="duplicate")
        raise
    return _assignment_payload(row, outcome=outcome)


def record_exposure(session: Session, experiment_id: int, *, student_key: str, status: str) -> dict:
    """记录曝光结果。分配先于展示：曝光失败/未曝光都保留分配事实，仍计入分析。"""
    if status not in ("success", "failed"):
        raise DomainError("status 必须是 success 或 failed")
    exp = get_experiment(session, experiment_id)
    a = session.scalars(
        select(Assignment).where(
            Assignment.experiment_id == exp.id,
            Assignment.student_key == student_key,
            Assignment.status == "assigned",
        )
    ).first()
    if a is None:
        raise DomainError("该学员在此实验中没有已分配记录", 404)
    a.exposure_status = status
    a.exposure_at = clock.now(session)
    # 分层实验：登记共同曝光事实（不同层可共存；同层互斥不会出现同学员两条）
    if a.layer_id is not None:
        layers.record_co_exposure(
            session,
            student_key=a.student_key,
            layer_id=a.layer_id,
            experiment_id=exp.id,
            variant=a.variant,
            status=status,
        )
    session.commit()
    return _assignment_payload(a, outcome="assigned")
