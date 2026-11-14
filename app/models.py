"""数据库模型。

设计要点：
- 分配（assignments）保存"首次事实"：同一实验同一学员仅一行，重复请求返回原结果。
- 事件（events）为不可变事实流，按 (source, biz_id, seq) 去重；全局自增 id 即"事件可用序号"，
  分析运行时对其做快照，晚报事件只属于更新的分析。
- 分析运行（analysis_runs）冻结截止点、事件序号快照、上报水位快照与比例配置快照，
  结果落库后不可变，可断点续算同一批次。
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

VARIANTS = ("control", "treatment")


class Clock(Base):
    """可推进虚拟时钟（单行，id=1）。系统内所有"现在"均取自此处。"""

    __tablename__ = "clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    now: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    seed: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enrolling")  # enrolling | stopped
    audience_rule: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enrollment_definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    observation_days: Mapped[int] = mapped_column(Integer, nullable=False)
    chi2_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.001)
    # 绑定互斥层；为空表示不受层管的独立实验（走单实验分流逻辑）
    layer_id: Mapped[int | None] = mapped_column(ForeignKey("layers.id"), nullable=True, index=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class Layer(Base):
    """互斥层：同一学员在一层内至多进入一个实验；不同层允许共存（不承诺互不影响）。

    revision 为层路由的乐观锁版本号：每次有效修订（容量申请/停招释放）+1，
    并发申请以条件更新线性化，只接受一个有效修订。
    """

    __tablename__ = "layers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    seed: Mapped[str] = mapped_column(String(128), nullable=False)  # 层哈希盐，创建后不可改
    total_buckets: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class LayerExperiment(Base):
    """实验在层内占用的容量区间 [bucket_from, bucket_to) 与当前状态。

    扩流只允许向两侧扩大（包含旧区间），新边界不得驱逐已分配学员；
    停招后状态置 stopped 并释放区间（行保留以追溯），释放范围可被新实验申请。
    """

    __tablename__ = "layer_experiments"
    __table_args__ = (
        UniqueConstraint("layer_id", "experiment_id", name="uq_layer_experiment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    layer_id: Mapped[int] = mapped_column(ForeignKey("layers.id"), nullable=False, index=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id"), nullable=False, unique=True
    )
    bucket_from: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_to: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)  # 最近一次区间修订后的层 revision
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enrolling")  # enrolling | stopped
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class LayerRevision(Base):
    """层路由修订历史：每个 revision 冻结当时的全量区间快照。

    历史重放按当时快照解释旧入组，不能用新边界重判；分配事实同时自带 route_snapshot。
    """

    __tablename__ = "layer_revisions"
    __table_args__ = (
        UniqueConstraint("layer_id", "revision", name="uq_layer_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    layer_id: Mapped[int] = mapped_column(ForeignKey("layers.id"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_note: Mapped[str] = mapped_column(String(256), nullable=False)
    snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class CoExposure(Base):
    """跨层共同曝光事实：学员在某层某实验发生曝光回执时落一行（成功/失败均记录）。

    只陈述"同一学员在共存层中分别被曝光"的事实，不承诺层间互不影响；
    分析侧不提供事后剔除重叠学员的入口（ITT 保持全量）。
    """

    __tablename__ = "co_exposures"
    __table_args__ = (
        UniqueConstraint("student_key", "experiment_id", name="uq_coexposure_student_experiment"),
        Index("ix_coexposure_student", "student_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False)
    layer_id: Mapped[int] = mapped_column(ForeignKey("layers.id"), nullable=False, index=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    variant: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exposure_status: Mapped[str] = mapped_column(String(16), nullable=False)  # success | failed
    recorded_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class RatioConfig(Base):
    """分流比例配置；实验期间调整只产生新行，仅作用于生效之后的分配。"""

    __tablename__ = "ratio_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    effective_from: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    ratios: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {"control": w, "treatment": w}


class Student(Base):
    """稳定学员标识。student_key 由调用方提供或由系统生成，跨设备不变。"""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class StudentDevice(Base):
    """设备到学员的绑定；同一学员换设备仍解析到同一 student_key。"""

    __tablename__ = "student_devices"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    first_seen_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Assignment(Base):
    """分配事实（含资格不足/停招等未分配决定的首次记录）。"""

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("experiment_id", "student_key", name="uq_assignment_exp_student"),
        UniqueConstraint("experiment_id", "request_id", name="uq_assignment_exp_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # assigned | ineligible | layer_not_selected | stopped
    variant: Mapped[str | None] = mapped_column(String(16), nullable=True)  # control | treatment
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    eligibility: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 首次申请时的资料与逐条判定
    ratio_config_id: Mapped[int | None] = mapped_column(ForeignKey("ratio_configs.id"), nullable=True)
    # 分层路由：层、层内桶位、命中时的层 revision 与当时全量路由/实验配置快照
    layer_id: Mapped[int | None] = mapped_column(ForeignKey("layers.id"), nullable=True, index=True)
    layer_bucket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    exposure_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")  # unknown | success | failed
    exposure_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Source(Base):
    """行为来源及其"已完整上报"的时间界限（水位线）。"""

    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    complete_until: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class Event(Base):
    """不可变行为事件。id 为全局摄取序号（分析快照即 max(id)）。"""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("source", "biz_id", "seq", name="uq_event_source_biz_seq"),
        Index("ix_events_student", "student_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    biz_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    student_key: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ingested_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalysisRun(Base):
    """一次分析批次：冻结截止点/事件序号/水位线/比例快照。中断后可按原参数续算。"""

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), nullable=False, index=True)
    cutoff: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    event_seq_snapshot: Mapped[int] = mapped_column(BigInteger, nullable=False)
    watermark_snapshot: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    ratio_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 运行时预先声明的分层结果快照：实验所属层、层内区间与层 revision；
    # 分析者只能查看该声明，不能看完效果再剔除跨层重叠学员
    layer_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")  # running | done
    # 汇总统计（完成时写入）
    diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ok | insufficient_counts | chi2_paused
    evidence_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    chi2_stat: Mapped[float | None] = mapped_column(Float, nullable=True)
    chi2_df: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chi2_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    chi2_paused: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chi2_details: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reconciled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisResult(Base):
    """每个运行 × 变体的分段计数。"""

    __tablename__ = "analysis_results"

    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    variant: Mapped[str] = mapped_column(String(16), primary_key=True)
    total_assigned: Mapped[int] = mapped_column(Integer, nullable=False)
    obs_insufficient: Mapped[int] = mapped_column(Integer, nullable=False)
    report_incomplete: Mapped[int] = mapped_column(Integer, nullable=False)
    mature: Mapped[int] = mapped_column(Integer, nullable=False)
    mature_success: Mapped[int] = mapped_column(Integer, nullable=False)
    mature_failure: Mapped[int] = mapped_column(Integer, nullable=False)


class AnalysisStudent(Base):
    """每个运行中学员所属分段与报名依据（用于对账与回查）。"""

    __tablename__ = "analysis_students"

    run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    variant: Mapped[str] = mapped_column(String(16), nullable=False)
    section: Mapped[str] = mapped_column(String(24), nullable=False)  # obs_insufficient | report_incomplete | mature_success | mature_failure
    window_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    exposure_status: Mapped[str] = mapped_column(String(16), nullable=False)
    qualifying_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    qualifying_biz_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    basis_event_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
