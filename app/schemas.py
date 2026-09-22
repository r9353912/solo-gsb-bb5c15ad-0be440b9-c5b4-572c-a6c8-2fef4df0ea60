"""Pydantic request/response schemas.

Times are accepted as IANA-zone- or fixed-offset-aware ISO 8601 strings and
returned normalized to UTC (``...Z``). Naive datetimes are rejected so the
timezone of every report is explicit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_aware(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("invalid datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include an explicit timezone, e.g. 2026-09-22T08:00:00+08:00")
    # Keep the original timezone representation; storage normalizes to UTC.
    return value


class EventCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, examples=["sensor-A-07"])
    start_time: datetime
    end_time: datetime
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sound_type: str | None = Field(default=None, max_length=128)
    observed_at: datetime | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _aware_time(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @field_validator("observed_at")
    @classmethod
    def _aware_optional(cls, v: datetime | None) -> datetime | None:
        return _ensure_aware(v) if v is not None else None

    @field_validator("end_time")
    @classmethod
    def _order(cls, v: datetime, info) -> datetime:  # noqa: ANN001
        start = info.data.get("start_time")
        if start is not None and v < start:
            raise ValueError("end_time must be equal to or later than start_time")
        return v


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    start_time: datetime
    end_time: datetime
    start_time_raw: str
    end_time_raw: str
    latitude: float
    longitude: float
    cell_x: int
    cell_y: int
    confidence: float
    sound_type: str | None
    tz_offset_minutes: int
    cluster_id: int | None
    observed_at: datetime | None
    created_at: datetime


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cluster_id: int
    seq: int
    action: str
    event_id: int | None
    other_cluster_id: int | None
    device_id: str | None
    detail: str | None
    created_at: datetime


class ClusterOut(BaseModel):
    id: int
    cell_x: int
    cell_y: int
    device_ids: list[str]
    representative_event_id: int | None
    confidence: float
    event_count: int
    started_at: datetime
    ended_at: datetime
    active: bool
    merged_into_id: int | None
    created_at: datetime
    updated_at: datetime


class ClusterDetail(ClusterOut):
    members: list[EventOut]
    audit_chain: list[AuditOut]
    lineage: list[int]


class IngestResult(BaseModel):
    created: bool
    cluster_id: int
    event_id: int
    merged_cluster_ids: list[int]


class RebuildResult(BaseModel):
    cluster_count: int
    event_count: int


class HealthOut(BaseModel):
    status: str
    database: str
