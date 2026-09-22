"""Pydantic request/response schemas.

Times are accepted as IANA-zone- or fixed-offset-aware ISO 8601 strings and
returned normalized to UTC (``...Z``). Naive datetimes are rejected so the
timezone of every report is explicit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


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
    # Geofence enter/exit alerts triggered by this event (usually empty).
    geofence_alerts: list["GeofenceAlertOut"] = Field(default_factory=list)


class RebuildResult(BaseModel):
    cluster_count: int
    event_count: int


class HealthOut(BaseModel):
    status: str
    database: str


# ---------------------------------------------------------------------------
# Geofences
# ---------------------------------------------------------------------------

DeviceId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
# Polygon positions are [longitude, latitude] pairs (GeoJSON order).
Position = tuple[float, float]


def _check_position(lon: float, lat: float) -> None:
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude {lon} out of range [-180, 180]")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude {lat} out of range [-90, 90]")


class GeofenceCreate(BaseModel):
    """Outer ring plus optional holes; rings may be closed (first == last)."""

    name: str = Field(min_length=1, max_length=128, examples=["yard-north"])
    outer: list[Position] = Field(min_length=3)
    holes: list[list[Position]] = Field(default_factory=list)
    device_ids: list[DeviceId] = Field(default_factory=list)

    @field_validator("outer")
    @classmethod
    def _outer_positions(cls, value: list[Position]) -> list[Position]:
        for lon, lat in value:
            _check_position(lon, lat)
        return value

    @field_validator("holes")
    @classmethod
    def _hole_positions(cls, value: list[list[Position]]) -> list[list[Position]]:
        for ring in value:
            if len(ring) < 3:
                raise ValueError("each hole must have at least 3 vertices")
            for lon, lat in ring:
                _check_position(lon, lat)
        return value


class GeofenceUpdate(GeofenceCreate):
    """Full replacement of a geofence (PUT semantics)."""


class DeviceBind(BaseModel):
    device_id: DeviceId


class GeofenceOut(BaseModel):
    id: int
    name: str
    outer: list[Position]
    holes: list[list[Position]]
    boundary_rule: str
    device_ids: list[str]
    alert_count: int
    created_at: datetime
    updated_at: datetime


class GeofenceAlertOut(BaseModel):
    id: int
    fence_id: int
    fence_name: str | None
    device_id: str
    event_id: int | None
    transition: str  # enter | exit
    occurred_at: datetime
    created_at: datetime


class GeofenceAuditOut(BaseModel):
    id: int
    fence_id: int
    seq: int
    action: str
    device_id: str | None
    detail: str | None
    created_at: datetime


class ReplayResult(BaseModel):
    fences_processed: int
    pairs_evaluated: int
    alert_count: int
