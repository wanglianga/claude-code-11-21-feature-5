"""FastAPI 入口：试听分流与报名效果分析服务。

接口分组：
- clock：可推进虚拟时钟
- experiments：实验登记（受众/种子/比例/报名定义/观察天数）、比例调整、停招
- assignments / exposures：分配申请（首次事实）与曝光回执
- events / sources：合成行为流摄取与上报水位线
- analysis：按统一截止点运行分析、断点续算、结果与学员明细查询
- students：稳定学员标识与按学员回查
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import analysis, clock, events as events_svc, layers, services
from .db import Base, get_engine, get_session_factory
from .models import AnalysisRun, Assignment, Experiment, Layer, Source, Student
from .schemas import (
    AssignmentRequest,
    ClockAdvanceRequest,
    EventIngestRequest,
    ExperimentCreateRequest,
    ExposureRequest,
    LayerCreateRequest,
    LayerSlotRequest,
    RatioChangeRequest,
    StudentRegisterRequest,
    WatermarkItem,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    Base.metadata.create_all(engine)
    session_factory = get_session_factory()
    with session_factory() as s:
        clock.ensure_clock(s)
    yield


app = FastAPI(
    title="试听分流与报名效果分析服务",
    version="1.0.0",
    description=(
        "从首次分配开始固定比较对象，在观察时间相同的前提下评估新试听安排对整体报名的影响。"
        "全部时间统一 UTC；观察窗口左闭右开；分析按首次分配全量（ITT）进行。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(services.DomainError)
async def domain_error_handler(request: Request, exc: services.DomainError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


def get_db():
    session_factory = get_session_factory()
    with session_factory() as s:
        yield s


# ---------------------------------------------------------------- 时钟

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/clock", tags=["clock"])
def get_clock(db: Session = Depends(get_db)):
    return {"now": clock.now(db).isoformat(), "timezone": "UTC"}


@app.post("/clock/advance", tags=["clock"])
def advance_clock(body: ClockAdvanceRequest, db: Session = Depends(get_db)):
    try:
        now = clock.advance(db, seconds=body.seconds, to=body.to)
    except ValueError as e:
        raise services.DomainError(str(e), 400)
    return {"now": now.isoformat(), "timezone": "UTC"}


# ---------------------------------------------------------------- 实验

@app.post("/experiments", status_code=201, tags=["experiments"])
def create_experiment(body: ExperimentCreateRequest, db: Session = Depends(get_db)):
    exp = services.create_experiment(
        db,
        name=body.name,
        seed=body.seed,
        observation_days=body.observation_days,
        ratios=body.ratios,
        audience_rule=body.audience_rule,
        enrollment_definition=body.enrollment_definition,
        chi2_threshold=body.chi2_threshold,
        layer_id=body.layer_id,
    )
    return experiment_detail(exp.id, db)


@app.get("/experiments", tags=["experiments"])
def list_experiments(db: Session = Depends(get_db)):
    rows = db.scalars(select(Experiment).order_by(Experiment.id))
    return [
        {
            "id": e.id,
            "name": e.name,
            "status": e.status,
            "observation_days": e.observation_days,
            "layer_id": e.layer_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


@app.get("/experiments/{experiment_id}", tags=["experiments"])
def experiment_detail(experiment_id: int, db: Session = Depends(get_db)):
    exp = services.get_experiment(db, experiment_id)
    configs = services.list_ratio_configs(db, exp.id)
    counts = dict(
        db.execute(
            select(Assignment.status, func.count())
            .where(Assignment.experiment_id == exp.id)
            .group_by(Assignment.status)
        ).all()
    )
    return {
        "id": exp.id,
        "name": exp.name,
        "seed": exp.seed,
        "status": exp.status,
        "audience_rule": exp.audience_rule,
        "enrollment_definition": exp.enrollment_definition,
        "observation_days": exp.observation_days,
        "chi2_threshold": exp.chi2_threshold,
        "layer_id": exp.layer_id,
        "created_at": exp.created_at.isoformat(),
        "stopped_at": exp.stopped_at.isoformat() if exp.stopped_at else None,
        "ratio_configs": [
            {"id": c.id, "effective_from": c.effective_from.isoformat(), "ratios": c.ratios}
            for c in configs
        ],
        "assignment_counts": {
            "assigned": counts.get("assigned", 0),
            "ineligible": counts.get("ineligible", 0),
            "layer_not_selected": counts.get("layer_not_selected", 0),
            "stopped": counts.get("stopped", 0),
        },
    }


@app.post("/experiments/{experiment_id}/ratio", tags=["experiments"])
def change_ratio(experiment_id: int, body: RatioChangeRequest, db: Session = Depends(get_db)):
    rc = services.change_ratio(db, experiment_id, body.ratios)
    return {
        "id": rc.id,
        "experiment_id": rc.experiment_id,
        "effective_from": rc.effective_from.isoformat(),
        "ratios": rc.ratios,
        "note": "新比例仅作用于生效之后的分配；历史分配仍归属原配置期间",
    }


@app.post("/experiments/{experiment_id}/stop", tags=["experiments"])
def stop_experiment(experiment_id: int, db: Session = Depends(get_db)):
    exp = services.stop_experiment(db, experiment_id)
    return {
        "id": exp.id,
        "status": exp.status,
        "stopped_at": exp.stopped_at.isoformat() if exp.stopped_at else None,
        "note": "停招只停新分配；既有学员的观察与分析继续",
    }


# ---------------------------------------------------------------- 互斥层

@app.post("/layers", status_code=201, tags=["layers"])
def create_layer(body: LayerCreateRequest, db: Session = Depends(get_db)):
    layer = layers.create_layer(
        db, name=body.name, seed=body.seed, total_buckets=body.total_buckets
    )
    return layers.layer_detail(db, layer)


@app.get("/layers", tags=["layers"])
def list_layers(db: Session = Depends(get_db)):
    rows = db.scalars(select(Layer).order_by(Layer.id))
    return [
        {
            "id": layer.id,
            "name": layer.name,
            "seed": layer.seed,
            "total_buckets": layer.total_buckets,
            "revision": layer.revision,
            "status": layer.status,
            "created_at": layer.created_at.isoformat(),
        }
        for layer in rows
    ]


@app.get("/layers/{layer_id}", tags=["layers"])
def get_layer(layer_id: int, db: Session = Depends(get_db)):
    return layers.layer_detail(db, layers.get_layer(db, layer_id))


@app.post("/layers/{layer_id}/experiments/{experiment_id}/slot", tags=["layers"])
def attach_slot(layer_id: int, experiment_id: int, body: LayerSlotRequest,
                db: Session = Depends(get_db)):
    """初次申请层内容量区间；expected_revision 做乐观锁，区间重叠/超额返回 409。"""
    layer = layers.get_layer(db, layer_id)
    exp = services.get_experiment(db, experiment_id)
    le = layers.attach_experiment(
        db, layer, exp,
        bucket_from=body.bucket_from, bucket_to=body.bucket_to,
        expected_revision=body.expected_revision,
    )
    return _slot_payload(db, layer_id, le)


@app.put("/layers/{layer_id}/experiments/{experiment_id}/capacity", tags=["layers"])
def expand_capacity(layer_id: int, experiment_id: int, body: LayerSlotRequest,
                    db: Session = Depends(get_db)):
    """扩流：新区间必须包含旧区间，只占用未分配桶位，不驱逐任何已分配学员。"""
    layer = layers.get_layer(db, layer_id)
    le = layers.apply_capacity(
        db, layer, experiment_id,
        bucket_from=body.bucket_from, bucket_to=body.bucket_to,
        expected_revision=body.expected_revision,
    )
    return _slot_payload(db, layer_id, le)


def _slot_payload(db: Session, layer_id: int, le):
    return {
        "layer_id": layer_id,
        "experiment_id": le.experiment_id,
        "bucket_from": le.bucket_from,
        "bucket_to": le.bucket_to,
        "capacity": le.bucket_to - le.bucket_from,
        "status": le.status,
        "revision": le.revision,
        "layer": layers.layer_detail(db, layers.get_layer(db, layer_id)),
        "note": "修订已线性化：并发申请只有一个生效，失败方需按新 revision 重新读取剩余容量",
    }


@app.get("/layers/{layer_id}/revisions", tags=["layers"])
def get_revisions(layer_id: int, db: Session = Depends(get_db)):
    rows = layers.list_revisions(db, layer_id)
    return [
        {
            "revision": r.revision,
            "change_note": r.change_note,
            "created_at": r.created_at.isoformat(),
            "snapshot": r.snapshot,
        }
        for r in rows
    ]


@app.get("/layers/{layer_id}/revisions/{revision}", tags=["layers"])
def get_revision(layer_id: int, revision: int, db: Session = Depends(get_db)):
    r = layers.get_revision_snapshot(db, layer_id, revision)
    return {
        "layer_id": layer_id,
        "revision": r.revision,
        "change_note": r.change_note,
        "created_at": r.created_at.isoformat(),
        "snapshot": r.snapshot,
        "note": "历史重放只能使用当时 revision 的路由解释旧入组，不能用新边界重判",
    }


@app.get("/layers/{layer_id}/explain", tags=["layers"])
def explain_routing(layer_id: int, student_key: str = Query(...),
                    db: Session = Depends(get_db)):
    """解释某学员在当前路由下命中哪个实验或为何未入选（不落库）。"""
    return layers.explain(db, layer_id, student_key)


@app.get("/reports/co-exposures", tags=["layers"])
def co_exposure_report(db: Session = Depends(get_db)):
    """跨层碰撞报告：共同分组及曝光人数；只陈述共存事实，不提供事后剔除入口。"""
    return layers.co_exposure_report(db)


# ---------------------------------------------------------------- 学员与分配

@app.post("/students", status_code=201, tags=["students"])
def register_student(body: StudentRegisterRequest, db: Session = Depends(get_db)):
    student, created = services.register_student(
        db, student_key=body.student_key, device_id=body.device_id, profile=body.profile
    )
    db.commit()
    return {
        "student_key": student.student_key,
        "profile": student.profile,
        "created": created,
        "created_at": student.created_at.isoformat(),
    }


@app.post("/experiments/{experiment_id}/assignments", tags=["assignments"])
def request_assignment(experiment_id: int, body: AssignmentRequest, db: Session = Depends(get_db)):
    return services.request_assignment(
        db,
        experiment_id,
        request_id=body.request_id,
        student_key=body.student_key,
        device_id=body.device_id,
        profile=body.profile,
    )


@app.post("/experiments/{experiment_id}/exposures", tags=["assignments"])
def record_exposure(experiment_id: int, body: ExposureRequest, db: Session = Depends(get_db)):
    return services.record_exposure(
        db, experiment_id, student_key=body.student_key, status=body.status
    )


@app.get("/experiments/{experiment_id}/students/{student_key}", tags=["students"])
def get_student_view(experiment_id: int, student_key: str, db: Session = Depends(get_db)):
    return analysis.student_view(db, experiment_id, student_key)


# ---------------------------------------------------------------- 行为流

@app.post("/events", tags=["events"])
def ingest_events(body: EventIngestRequest, db: Session = Depends(get_db)):
    return events_svc.ingest_events(
        db,
        [e.model_dump() for e in body.events],
        [w.model_dump() for w in body.watermarks],
    )


@app.get("/sources", tags=["events"])
def list_sources(db: Session = Depends(get_db)):
    rows = db.scalars(select(Source).order_by(Source.name))
    return {
        "sources": [
            {
                "name": s.name,
                "complete_until": s.complete_until.isoformat(),
                "updated_at": s.updated_at.isoformat(),
            }
            for s in rows
        ],
        "effective_watermark": (
            wm.isoformat() if (wm := events_svc.effective_watermark(db)) else None
        ),
    }


@app.post("/sources/{source}/watermark", tags=["events"])
def update_watermark(source: str, body: WatermarkItem, db: Session = Depends(get_db)):
    """推进某来源"已完整上报"的时间界限（只允许前进）。"""
    if body.source != source:
        raise services.DomainError("路径与请求体中的 source 不一致", 400)
    row = events_svc.set_watermark(db, source, body.complete_until)
    db.commit()
    return {
        "name": row.name,
        "complete_until": row.complete_until.isoformat(),
        "effective_watermark": (
            wm.isoformat() if (wm := events_svc.effective_watermark(db)) else None
        ),
    }


# ---------------------------------------------------------------- 分析

@app.post("/experiments/{experiment_id}/analysis", status_code=201, tags=["analysis"])
def run_analysis(experiment_id: int, db: Session = Depends(get_db)):
    """以当前时钟为统一截止点运行一次分析；冻结事件序号与水位线快照。"""
    run = analysis.create_run(db, experiment_id)
    analysis.compute_run(db, run)
    return analysis.run_result(db, run.id)


@app.post("/analysis/{run_id}/resume", tags=["analysis"])
def resume_analysis(run_id: int, db: Session = Depends(get_db)):
    """断点续算：按运行创建时冻结的截止点与快照完成同一批次。"""
    analysis.resume_run(db, run_id)
    return analysis.run_result(db, run_id)


@app.get("/experiments/{experiment_id}/analysis", tags=["analysis"])
def list_analysis(experiment_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.experiment_id == experiment_id)
        .order_by(AnalysisRun.id)
    )
    return [
        {
            "run_id": r.id,
            "status": r.status,
            "cutoff": r.cutoff.isoformat(),
            "event_seq_snapshot": r.event_seq_snapshot,
            "watermark_snapshot": r.watermark_snapshot.isoformat() if r.watermark_snapshot else None,
            "evidence_status": r.evidence_status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/analysis/{run_id}", tags=["analysis"])
def get_analysis(run_id: int, db: Session = Depends(get_db)):
    return analysis.run_result(db, run_id)


@app.get("/analysis/{run_id}/students", tags=["analysis"])
def get_analysis_students(
    run_id: int,
    section: str | None = Query(default=None, description="obs_insufficient | report_incomplete | mature_success | mature_failure"),
    variant: str | None = Query(default=None, description="control | treatment"),
    limit: int = Query(default=1000, le=100000),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    return analysis.run_students(
        db, run_id, section=section, variant=variant, limit=limit, offset=offset
    )
