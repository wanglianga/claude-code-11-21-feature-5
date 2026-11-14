"""互斥层与容量区间管理。

流量模型
--------
- 每个层把 [0, total_buckets) 的桶位空间切给若干实验，区间互不重叠；
  学员按稳定标识哈希到固定桶位（与 revision 无关，永不因扩流挪动），
  先在层内选中"桶位所属实验"，再按该实验自己的种子/比例进入分组。
- 同一学员在一层内至多进入一个实验（互斥）；不同层允许共存，
  共存事实由 co_exposures 记录，系统不承诺层间互不影响。

容量修订
--------
- 扩流只能扩大区间（新区间包含旧区间），只动用未分配桶位，新边界不驱逐老成员；
- 停招释放区间，老成员保留原分配完成观察，释放范围仅对新来者可用；
- 每次修订先对层行加行锁，再校验 expected_revision 与区间重叠/总额，
  通过后 revision+1 并写入 LayerRevision 快照；并发申请只可能有一个成功，其余 409。

历史重放
--------
- 每次分配快照当时的全量路由（revision、桶位、区间、实验配置与比例），
  重放请求返回首次决定；解释旧入组只能用当时快照，不能拿新边界重判。
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock
from .errors import DomainError
from .models import (
    Assignment,
    CoExposure,
    Experiment,
    Layer,
    LayerExperiment,
    LayerRevision,
)

# 未入组原因（三类分别记录，不用一条失败代替）
REASON_AUDIENCE = "audience_rule_not_matched"  # 受众不合格
REASON_LAYER_MISS = "layer_not_selected"       # 层内未命中（桶位无实验或属其他实验）
REASON_STOPPED = "experiment_stopped"          # 实验停止

STATUS_LAYER_MISS = "layer_not_selected"


# ---------------------------------------------------------------- 哈希

def layer_bucket(seed: str, student_key: str, total_buckets: int) -> int:
    """学员在层内的稳定桶位：只取决于层种子与稳定标识，修订版本不参与计算。"""
    digest = hashlib.sha256(f"layer:{seed}:{student_key}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % total_buckets


# ---------------------------------------------------------------- 层与区间

def create_layer(session: Session, *, name: str, seed: str, total_buckets: int = 10000) -> Layer:
    if not name:
        raise DomainError("name 不能为空")
    if not seed:
        raise DomainError("seed 不能为空（层哈希盐，创建后不可改）")
    if not isinstance(total_buckets, int) or total_buckets < 100:
        raise DomainError("total_buckets 必须是 >=100 的整数")
    now = clock.now(session)
    layer = Layer(name=name, seed=str(seed), total_buckets=total_buckets,
                  revision=0, status="active", created_at=now)
    session.add(layer)
    try:
        session.flush()
    except IntegrityError as e:
        session.rollback()
        raise DomainError(f"层名称已存在: {name}", 409) from e
    session.add(LayerRevision(
        layer_id=layer.id, revision=0, change_note="layer_created",
        snapshot=[], created_at=now,
    ))
    session.commit()
    session.refresh(layer)
    return layer


def get_layer(session: Session, layer_id: int) -> Layer:
    layer = session.get(Layer, layer_id)
    if layer is None:
        raise DomainError(f"互斥层不存在: {layer_id}", 404)
    return layer


def _lock_layer(session: Session, layer_id: int) -> Layer:
    """行锁串行化同层并发修订；配合 expected_revision 做乐观锁双重保护。"""
    layer = session.scalars(
        select(Layer).where(Layer.id == layer_id).with_for_update()
    ).first()
    if layer is None:
        raise DomainError(f"互斥层不存在: {layer_id}", 404)
    return layer


def _enrolling_rows(session: Session, layer_id: int) -> list[LayerExperiment]:
    return list(session.scalars(
        select(LayerExperiment)
        .where(LayerExperiment.layer_id == layer_id,
               LayerExperiment.status == "enrolling")
        .order_by(LayerExperiment.bucket_from, LayerExperiment.id)
    ))


def _validate_interval(bucket_from: int, bucket_to: int, total_buckets: int) -> None:
    if not isinstance(bucket_from, int) or not isinstance(bucket_to, int):
        raise DomainError("bucket_from/bucket_to 必须是整数")
    if bucket_from < 0 or bucket_to > total_buckets or bucket_from >= bucket_to:
        raise DomainError(
            f"区间必须满足 0 <= bucket_from < bucket_to <= {total_buckets}"
        )


def _check_overlap_and_total(rows: list[LayerExperiment], total_buckets: int) -> None:
    """检查在招区间两两不重叠且总额不超限。"""
    rows = sorted(rows, key=lambda r: r.bucket_from)
    used = 0
    prev_to = -1
    for r in rows:
        if r.bucket_from < prev_to:
            raise DomainError(
                f"层内区间重叠：实验 {r.experiment_id} 的 [{r.bucket_from},{r.bucket_to}) "
                f"与前序区间（结束于 {prev_to}）相交", 409,
            )
        prev_to = max(prev_to, r.bucket_to)
        used += r.bucket_to - r.bucket_from
    if used > total_buckets:
        raise DomainError(f"层内容量总额 {used} 超过层总桶位 {total_buckets}", 409)


def route_snapshot(session: Session, layer: Layer) -> list[dict]:
    """当前全量路由（含已停招行，注明状态），用于修订历史与分配快照。"""
    rows = session.scalars(
        select(LayerExperiment)
        .where(LayerExperiment.layer_id == layer.id)
        .order_by(LayerExperiment.bucket_from, LayerExperiment.id)
    )
    return [
        {
            "layer_experiment_id": r.id,
            "experiment_id": r.experiment_id,
            "bucket_from": r.bucket_from,
            "bucket_to": r.bucket_to,
            "status": r.status,
            "revision": r.revision,
        }
        for r in rows
    ]


def _increment_revision(session: Session, layer: Layer) -> int:
    """条件更新推进 revision（乐观锁）；返回新版本号，行级修订由调用方先落好再写快照。"""
    new_rev = layer.revision + 1
    updated = session.execute(
        update(Layer)
        .where(Layer.id == layer.id, Layer.revision == layer.revision)
        .values(revision=new_rev)
    ).rowcount
    if updated != 1:  # 极端并发下条件更新失败
        session.rollback()
        raise DomainError("层路由已被其他管理员修订，请基于最新 revision 重试", 409)
    layer.revision = new_rev
    return new_rev


def _write_revision_history(session: Session, layer: Layer, note: str, new_rev: int) -> None:
    """在区间行的 revision/状态全部落定后，冻结该版本的全量路由快照。"""
    session.add(LayerRevision(
        layer_id=layer.id, revision=new_rev, change_note=note,
        snapshot=route_snapshot(session, layer), created_at=clock.now(session),
    ))


def _check_expected(layer: Layer, expected_revision: int | None) -> None:
    if expected_revision is None:
        raise DomainError("expected_revision 必填：请先 GET 层详情获取当前 revision")
    if expected_revision != layer.revision:
        raise DomainError(
            f"路由修订冲突：期望 revision={expected_revision}，当前已为 {layer.revision}；"
            "只有一个申请能生效，请重新读取剩余容量后再申请",
            409,
        )


def attach_experiment(
    session: Session,
    layer: Layer,
    experiment: Experiment,
    *,
    bucket_from: int,
    bucket_to: int,
    expected_revision: int,
) -> LayerExperiment:
    """把实验挂到层内区间（初次申请容量）。"""
    layer = _lock_layer(session, layer.id)
    _check_expected(layer, expected_revision)
    _validate_interval(bucket_from, bucket_to, layer.total_buckets)
    if experiment.layer_id is not None and experiment.layer_id != layer.id:
        raise DomainError(f"实验 {experiment.id} 已绑定其他层，不能重复挂载")
    existing = session.scalars(
        select(LayerExperiment).where(LayerExperiment.experiment_id == experiment.id)
    ).first()
    if existing is not None:
        raise DomainError(f"实验 {experiment.id} 已在层内占用区间", 409)

    now = clock.now(session)
    le = LayerExperiment(
        layer_id=layer.id, experiment_id=experiment.id,
        bucket_from=bucket_from, bucket_to=bucket_to,
        revision=layer.revision + 1, status="enrolling",
        created_at=now, updated_at=now,
    )
    session.add(le)
    session.flush()
    _check_overlap_and_total(_enrolling_rows(session, layer.id), layer.total_buckets)
    new_rev = _increment_revision(session, layer)
    le.revision = new_rev
    experiment.layer_id = layer.id
    session.flush()
    _write_revision_history(
        session, layer, f"attach experiment {experiment.id} [{bucket_from},{bucket_to})", new_rev
    )
    session.commit()
    session.refresh(le)
    return le


def apply_capacity(
    session: Session,
    layer: Layer,
    experiment_id: int,
    *,
    bucket_from: int,
    bucket_to: int,
    expected_revision: int,
) -> LayerExperiment:
    """扩流：新区间必须包含旧区间（只增不减），且不得与其他在招区间重叠。"""
    layer = _lock_layer(session, layer.id)
    _check_expected(layer, expected_revision)
    _validate_interval(bucket_from, bucket_to, layer.total_buckets)
    le = session.scalars(
        select(LayerExperiment).where(
            LayerExperiment.layer_id == layer.id,
            LayerExperiment.experiment_id == experiment_id,
        )
    ).first()
    if le is None:
        raise DomainError(f"实验 {experiment_id} 不在该层内", 404)
    if le.status != "enrolling":
        raise DomainError("实验已停招，区间已释放，不能再扩流；如需流量请新开实验", 409)
    # 扩流只能从未分配空间增加：新区间必须完整包含旧区间，老成员桶位始终在内
    if bucket_from > le.bucket_from or bucket_to < le.bucket_to:
        raise DomainError(
            f"扩流只能扩大区间：旧区间 [{le.bucket_from},{le.bucket_to})，"
            f"申请新区间 [{bucket_from},{bucket_to}) 未包含旧区间（会驱逐已分配学员）",
            409,
        )
    old_from, old_to = le.bucket_from, le.bucket_to
    le.bucket_from, le.bucket_to = bucket_from, bucket_to
    le.updated_at = clock.now(session)
    session.flush()
    _check_overlap_and_total(_enrolling_rows(session, layer.id), layer.total_buckets)
    new_rev = _increment_revision(session, layer)
    le.revision = new_rev
    session.flush()
    _write_revision_history(
        session, layer,
        f"expand experiment {experiment_id} [{old_from},{old_to}) -> [{bucket_from},{bucket_to})",
        new_rev,
    )
    session.commit()
    session.refresh(le)
    return le


def release_slot(session: Session, layer: Layer, experiment_id: int) -> LayerExperiment | None:
    """停招释放：标记层内区间为 stopped 并推进 revision（调用方负责实验本体停招与提交）。

    实验建在层内但从未申请到区间时无行可释放，返回 None（不影响实验本体停招）。
    """
    layer = _lock_layer(session, layer.id)
    le = session.scalars(
        select(LayerExperiment).where(
            LayerExperiment.layer_id == layer.id,
            LayerExperiment.experiment_id == experiment_id,
        )
    ).first()
    if le is None or le.status == "stopped":
        return le
    le.status = "stopped"
    le.stopped_at = clock.now(session)
    le.updated_at = le.stopped_at
    session.flush()
    new_rev = _increment_revision(session, layer)
    le.revision = new_rev
    session.flush()
    _write_revision_history(
        session, layer,
        f"stop experiment {experiment_id}; release [{le.bucket_from},{le.bucket_to})",
        new_rev,
    )
    return le


def list_revisions(session: Session, layer_id: int) -> list[LayerRevision]:
    get_layer(session, layer_id)
    return list(session.scalars(
        select(LayerRevision)
        .where(LayerRevision.layer_id == layer_id)
        .order_by(LayerRevision.revision)
    ))


def get_revision_snapshot(session: Session, layer_id: int, revision: int) -> LayerRevision:
    row = session.scalars(
        select(LayerRevision).where(
            LayerRevision.layer_id == layer_id, LayerRevision.revision == revision
        )
    ).first()
    if row is None:
        raise DomainError(f"层 {layer_id} 不存在 revision={revision}", 404)
    return row


# ---------------------------------------------------------------- 层内决策

def select_layer_experiment(
    session: Session, layer: Layer, student_key: str
) -> tuple[int, LayerExperiment | None, list[LayerExperiment]]:
    """按稳定桶位选择层内在招实验。返回 (bucket, 命中行或 None, 在招区间列表)。"""
    bucket = layer_bucket(layer.seed, student_key, layer.total_buckets)
    rows = _enrolling_rows(session, layer.id)
    hit = next((r for r in rows if r.bucket_from <= bucket < r.bucket_to), None)
    return bucket, hit, rows


def layer_detail(session: Session, layer: Layer) -> dict:
    rows = list(session.scalars(
        select(LayerExperiment)
        .where(LayerExperiment.layer_id == layer.id)
        .order_by(LayerExperiment.status, LayerExperiment.bucket_from)
    ))
    enrolling = [r for r in rows if r.status == "enrolling"]
    used = sum(r.bucket_to - r.bucket_from for r in enrolling)
    return {
        "id": layer.id,
        "name": layer.name,
        "seed": layer.seed,
        "total_buckets": layer.total_buckets,
        "revision": layer.revision,
        "status": layer.status,
        "created_at": layer.created_at.isoformat(),
        "capacity": {
            "used": used,
            "free": layer.total_buckets - used,
            "total": layer.total_buckets,
        },
        "experiments": [
            {
                "experiment_id": r.experiment_id,
                "bucket_from": r.bucket_from,
                "bucket_to": r.bucket_to,
                "capacity": r.bucket_to - r.bucket_from,
                "status": r.status,
                "revision": r.revision,
                "stopped_at": r.stopped_at.isoformat() if r.stopped_at else None,
            }
            for r in rows
        ],
    }


def explain(session: Session, layer_id: int, student_key: str) -> dict:
    """解释某学员在当前路由下为什么命中/未命中（不落库）。"""
    layer = get_layer(session, layer_id)
    bucket, hit, rows = select_layer_experiment(session, layer, student_key)
    intervals = [
        {"experiment_id": r.experiment_id, "bucket_from": r.bucket_from,
         "bucket_to": r.bucket_to, "contains_bucket": r.bucket_from <= bucket < r.bucket_to}
        for r in rows
    ]
    # 老成员黏性：已在同层某实验入组的学员不能再被其他实验接纳
    prior = session.scalars(
        select(Assignment)
        .join(Experiment, Assignment.experiment_id == Experiment.id)
        .where(Assignment.student_key == student_key,
               Experiment.layer_id == layer.id,
               Assignment.status == "assigned")
        .order_by(Assignment.id)
    ).first()
    if hit is None:
        reason = REASON_LAYER_MISS
        detail = ("桶位未被任何在招实验区间覆盖（层内未命中）；"
                  "扩流后仅新请求的学员可进入新范围，已产生的首次决定不重判")
        selected = None
    else:
        reason = None
        detail = "桶位落在在招区间内，进入该实验后再按实验种子分组"
        selected = hit.experiment_id
    if prior is not None:
        detail += (
            f"；该学员已在同层实验 {prior.experiment_id} 入组（状态 assigned），"
            "按老成员黏性，释放/新增容量仅新来者可用，对其他实验的申请会记为层内未命中"
        )
    return {
        "layer_id": layer.id,
        "revision": layer.revision,
        "student_key": student_key,
        "bucket": bucket,
        "total_buckets": layer.total_buckets,
        "selected_experiment_id": selected,
        "reason": reason,
        "detail": detail,
        "prior_assignment": (
            {"experiment_id": prior.experiment_id, "status": prior.status,
             "variant": prior.variant, "assigned_at": prior.assigned_at.isoformat()}
            if prior is not None else None
        ),
        "enrolling_intervals": intervals,
    }


# ---------------------------------------------------------------- 共同曝光

def record_co_exposure(
    session: Session,
    *,
    student_key: str,
    layer_id: int,
    experiment_id: int,
    variant: str | None,
    status: str,
) -> None:
    """登记跨层共同曝光事实（幂等：同学员同实验只保留首次回执，并发安全）。"""
    session.execute(
        pg_insert(CoExposure)
        .values(
            student_key=student_key,
            layer_id=layer_id,
            experiment_id=experiment_id,
            variant=variant,
            exposure_status=status,
            recorded_at=clock.now(session),
        )
        .on_conflict_do_nothing(constraint="uq_coexposure_student_experiment")
    )


def co_exposure_report(session: Session) -> dict:
    """跨层碰撞报告：只报告共同分组与曝光人数等事实，不提供事后剔除入口。"""
    experiments = list(session.scalars(select(Experiment).where(Experiment.layer_id.is_not(None))))
    per_experiment = {}
    for exp in experiments:
        rows = list(session.scalars(
            select(CoExposure).where(CoExposure.experiment_id == exp.id)
        ))
        if not rows:
            continue
        per_experiment[exp.id] = {
            "experiment_id": exp.id,
            "name": exp.name,
            "layer_id": exp.layer_id,
            "exposed_students": len({r.student_key for r in rows}),
            "success_students": len({r.student_key for r in rows if r.exposure_status == "success"}),
            "failed_students": len({r.student_key for r in rows if r.exposure_status == "failed"}),
        }

    pairs = []
    ids = sorted(per_experiment)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            exp_a, exp_b = session.get(Experiment, a), session.get(Experiment, b)
            if exp_a.layer_id == exp_b.layer_id:
                continue  # 同层互斥，理论上无重叠，跳过
            ra = {r.student_key: r for r in session.scalars(
                select(CoExposure).where(CoExposure.experiment_id == a))}
            rb = {r.student_key: r for r in session.scalars(
                select(CoExposure).where(CoExposure.experiment_id == b))}
            common = sorted(set(ra) & set(rb))
            if not common:
                continue
            by_variant_pair: dict[str, dict] = {}
            for key in common:
                vp = f"{ra[key].variant or 'none'}|{rb[key].variant or 'none'}"
                cell = by_variant_pair.setdefault(
                    vp, {"students": 0, "both_exposure_success": 0})
                cell["students"] += 1
                if ra[key].exposure_status == "success" and rb[key].exposure_status == "success":
                    cell["both_exposure_success"] += 1
            pairs.append({
                "experiment_a": {"experiment_id": a, "name": exp_a.name, "layer_id": exp_a.layer_id},
                "experiment_b": {"experiment_id": b, "name": exp_b.name, "layer_id": exp_b.layer_id},
                "overlap_students": len(common),
                "both_exposure_success": sum(
                    1 for k in common
                    if ra[k].exposure_status == "success" and rb[k].exposure_status == "success"
                ),
                "by_variant_pair": by_variant_pair,
                "student_keys": common,
                "note": "共同曝光为预先声明的共存层事实，分析保持 ITT 全量，不支持事后剔除重叠学员",
            })
    return {
        "experiments": [per_experiment[k] for k in ids if k in per_experiment],
        "pairs": pairs,
    }


def student_co_exposures(session: Session, student_key: str) -> dict:
    rows = list(session.scalars(
        select(CoExposure)
        .where(CoExposure.student_key == student_key)
        .order_by(CoExposure.recorded_at, CoExposure.id)
    ))
    return {
        "student_key": student_key,
        "co_exposures": [
            {
                "layer_id": r.layer_id,
                "experiment_id": r.experiment_id,
                "variant": r.variant,
                "exposure_status": r.exposure_status,
                "recorded_at": r.recorded_at.isoformat(),
            }
            for r in rows
        ],
        "distinct_layers": len({r.layer_id for r in rows}),
    }
