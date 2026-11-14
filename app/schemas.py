"""请求/响应模式（OpenAPI 文档由此自动生成）。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def _utc(v: datetime) -> datetime:
    """统一时区：无时区的输入按 UTC 解释，全部归一到 UTC。"""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


class ClockAdvanceRequest(BaseModel):
    seconds: float | None = Field(default=None, ge=0, description="向前推进的秒数")
    to: datetime | None = Field(default=None, description="推进到指定时刻（与 seconds 二选一）")

    @field_validator("to")
    @classmethod
    def _norm(cls, v):
        return _utc(v) if v else v


class ExperimentCreateRequest(BaseModel):
    name: str = Field(description="实验名（唯一）")
    seed: str = Field(description="固定种子，创建后不可改")
    observation_days: int = Field(ge=1, description="观察天数：入组时刻起连续若干天")
    ratios: dict = Field(description='分流比例，如 {"control": 0.5, "treatment": 0.5}')
    audience_rule: dict | None = Field(default=None, description="受众规则（创建后不可改；变更需新开实验）")
    enrollment_definition: dict | None = Field(default=None, description="报名定义：created_events / revoke_events")
    chi2_threshold: float = Field(default=0.001, gt=0, lt=1, description="分流卡方检查阈值")
    layer_id: int | None = Field(default=None, description="绑定的互斥层 id；不填则为不受层管的独立实验")


class LayerCreateRequest(BaseModel):
    name: str = Field(description="互斥层名称（唯一）")
    seed: str = Field(description="层哈希盐，创建后不可改；桶位只取决于种子与学员稳定标识")
    total_buckets: int = Field(default=10000, ge=100, description="层桶位空间大小（容量计量单位）")


class LayerSlotRequest(BaseModel):
    bucket_from: int = Field(ge=0, description="容量区间起始桶位（含）")
    bucket_to: int = Field(ge=1, description="容量区间结束桶位（不含）")
    expected_revision: int = Field(description="乐观锁：客户端读取到的层 revision，不一致返回 409")


class RatioChangeRequest(BaseModel):
    ratios: dict


class StudentRegisterRequest(BaseModel):
    student_key: str | None = None
    device_id: str | None = None
    profile: dict | None = None


class AssignmentRequest(BaseModel):
    request_id: str = Field(description="幂等键：同一 request_id 重放返回首次结果")
    student_key: str | None = Field(default=None, description="稳定学员标识；缺省时按设备解析或新建")
    device_id: str | None = None
    profile: dict | None = Field(default=None, description="首次申请时的资料（用于资格判定）")


class ExposureRequest(BaseModel):
    student_key: str
    status: str = Field(description="success | failed；曝光失败仍保留分配事实")


class EventItem(BaseModel):
    source: str
    biz_id: str
    seq: int
    event_type: str
    student_key: str
    occurred_at: datetime
    payload: dict | None = None

    @field_validator("occurred_at")
    @classmethod
    def _norm(cls, v):
        return _utc(v)


class WatermarkItem(BaseModel):
    source: str
    complete_until: datetime

    @field_validator("complete_until")
    @classmethod
    def _norm(cls, v):
        return _utc(v)


class EventIngestRequest(BaseModel):
    events: list[EventItem] = Field(default_factory=list)
    watermarks: list[WatermarkItem] = Field(default_factory=list)
