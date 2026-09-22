"""HTTP API routes for event ingestion, cluster queries and rebuild."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clustering
from .database import AuditEntry, Cluster, NoiseEvent, get_db
from .schemas import (
    AuditOut,
    ClusterDetail,
    ClusterOut,
    EventCreate,
    EventOut,
    HealthOut,
    IngestResult,
    RebuildResult,
)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _utc(value: datetime) -> datetime:
    """SQLite stores naive UTC; label it for JSON output."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _event_out(event: NoiseEvent) -> EventOut:
    data = EventOut.model_validate(event)
    data.start_time = _utc(data.start_time)
    data.end_time = _utc(data.end_time)
    if data.observed_at is not None:
        data.observed_at = _utc(data.observed_at)
    data.created_at = _utc(data.created_at)
    return data


def _audit_out(entry: AuditEntry) -> AuditOut:
    data = AuditOut.model_validate(entry)
    data.created_at = _utc(data.created_at)
    return data


def _cluster_out(cluster: Cluster) -> ClusterOut:
    return ClusterOut(
        id=cluster.id,
        cell_x=cluster.cell_x,
        cell_y=cluster.cell_y,
        device_ids=[d for d in cluster.device_ids.split(",") if d],
        representative_event_id=cluster.representative_event_id,
        confidence=cluster.confidence,
        event_count=cluster.event_count,
        started_at=_utc(cluster.started_at),
        ended_at=_utc(cluster.ended_at),
        active=cluster.active,
        merged_into_id=cluster.merged_into_id,
        created_at=_utc(cluster.created_at),
        updated_at=_utc(cluster.updated_at),
    )


def _store_event(db: Session, payload: EventCreate) -> tuple[NoiseEvent, list[int]]:
    event = clustering.make_event(payload.model_dump())
    db.add(event)
    db.flush()
    event, merged_ids = clustering.ingest_event(db, event)
    return event, merged_ids


# ---------------------------------------------------------------------------
# Health & ingestion
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthOut, tags=["meta"])
def health(db: Session = Depends(get_db)) -> HealthOut:
    db.execute(select(1))
    return HealthOut(status="ok", database="sqlite")


@router.post("/events", response_model=IngestResult, status_code=201, tags=["events"])
def ingest_single(
    payload: EventCreate, db: Session = Depends(get_db)
) -> IngestResult:
    event, merged_ids = _store_event(db, payload)
    return IngestResult(
        created=True,
        cluster_id=event.cluster_id,
        event_id=event.id,
        merged_cluster_ids=merged_ids,
    )


@router.post("/events/batch", response_model=list[IngestResult], status_code=201, tags=["events"])
def ingest_batch(
    payload: list[EventCreate], db: Session = Depends(get_db)
) -> list[IngestResult]:
    if not payload:
        raise HTTPException(status_code=422, detail="batch must contain at least one event")
    results: list[IngestResult] = []
    for item in payload:
        event, merged_ids = _store_event(db, item)
        results.append(
            IngestResult(
                created=True,
                cluster_id=event.cluster_id,
                event_id=event.id,
                merged_cluster_ids=merged_ids,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------

@router.get("/events", response_model=list[EventOut], tags=["events"])
def list_events(
    device_id: str | None = None,
    cluster_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[EventOut]:
    stmt = select(NoiseEvent).order_by(NoiseEvent.start_time, NoiseEvent.id)
    if device_id is not None:
        stmt = stmt.where(NoiseEvent.device_id == device_id)
    if cluster_id is not None:
        stmt = stmt.where(NoiseEvent.cluster_id == cluster_id)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [_event_out(row) for row in rows]


@router.get("/events/{event_id}", response_model=EventOut, tags=["events"])
def get_event(event_id: int, db: Session = Depends(get_db)) -> EventOut:
    event = db.get(NoiseEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    return _event_out(event)


# ---------------------------------------------------------------------------
# Cluster queries
# ---------------------------------------------------------------------------

def _parse_time(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{name} must be ISO 8601 with timezone, e.g. 2026-09-22T08:00:00+08:00",
        )
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


@router.get("/clusters", response_model=list[ClusterOut], tags=["clusters"])
def list_clusters(
    include_merged: bool = False,
    min_lat: float | None = Query(default=None, ge=-90, le=90),
    max_lat: float | None = Query(default=None, ge=-90, le=90),
    min_lon: float | None = Query(default=None, ge=-180, le=180),
    max_lon: float | None = Query(default=None, ge=-180, le=180),
    start_after: str | None = None,
    start_before: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ClusterOut]:
    stmt = select(Cluster).order_by(Cluster.started_at, Cluster.id)
    if not include_merged:
        stmt = stmt.where(Cluster.active.is_(True))
    if min_lat is not None or max_lat is not None or min_lon is not None or max_lon is not None:
        # Bounding box filter is applied on the member coordinates; evaluate
        # via clusters that have at least one member inside the box.
        member_stmt = select(NoiseEvent.cluster_id)
        if min_lat is not None:
            member_stmt = member_stmt.where(NoiseEvent.latitude >= min_lat)
        if max_lat is not None:
            member_stmt = member_stmt.where(NoiseEvent.latitude <= max_lat)
        if min_lon is not None:
            member_stmt = member_stmt.where(NoiseEvent.longitude >= min_lon)
        if max_lon is not None:
            member_stmt = member_stmt.where(NoiseEvent.longitude <= max_lon)
        stmt = stmt.where(Cluster.id.in_(member_stmt))

    t_start = _parse_time("start_after", start_after)
    t_end = _parse_time("start_before", start_before)
    if t_start is not None:
        stmt = stmt.where(Cluster.started_at >= t_start.replace(tzinfo=None))
    if t_end is not None:
        stmt = stmt.where(Cluster.started_at <= t_end.replace(tzinfo=None))

    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [_cluster_out(row) for row in rows]


def _resolve_cluster(db: Session, cluster_id: int) -> Cluster | None:
    cluster = db.get(Cluster, cluster_id)
    return cluster


def _lineage(db: Session, cluster: Cluster) -> list[int]:
    """Return all cluster ids in this cluster's provenance graph.

    * ancestors: clusters absorbed directly/transitively into this one
      (walked via ``merged_into_id`` pointers pointing here)
    * this cluster itself
    * descendants: clusters this cluster was later absorbed into, if it is
      itself inactive (walked forward via ``merged_into_id``)

    The detail endpoint aggregates members and audit rows across every id in
    the returned list, so the chain is visible from either endpoint.
    """
    seen: set[int] = set()

    # Walk backwards: BFS over clusters merged into known nodes.
    frontier = [cluster.id]
    while frontier:
        current_id = frontier.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        children = db.scalars(
            select(Cluster.id).where(Cluster.merged_into_id == current_id)
        ).all()
        frontier.extend(children)

    # Walk forwards to the final surviving cluster.
    current = cluster
    while current.merged_into_id is not None:
        parent = db.get(Cluster, current.merged_into_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        current = parent

    return sorted(seen)


@router.get("/clusters/{cluster_id}", response_model=ClusterDetail, tags=["clusters"])
def get_cluster(
    cluster_id: int,
    include_lineage: bool = Query(
        default=True, description="include members/audit rows of absorbed ancestor clusters"
    ),
    db: Session = Depends(get_db),
) -> ClusterDetail:
    cluster = _resolve_cluster(db, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"cluster {cluster_id} not found")

    lineage = _lineage(db, cluster)
    member_cluster_ids = lineage if include_lineage else [cluster.id]

    members = db.scalars(
        select(NoiseEvent)
        .where(NoiseEvent.cluster_id.in_(member_cluster_ids))
        .order_by(NoiseEvent.start_time, NoiseEvent.id)
    ).all()
    audit_rows = db.scalars(
        select(AuditEntry)
        .where(AuditEntry.cluster_id.in_(member_cluster_ids))
        .order_by(AuditEntry.cluster_id, AuditEntry.seq)
    ).all()

    base = _cluster_out(cluster)
    return ClusterDetail(
        **base.model_dump(),
        members=[_event_out(m) for m in members],
        audit_chain=[_audit_out(a) for a in audit_rows],
        lineage=lineage,
    )


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

@router.post("/clusters/rebuild", response_model=RebuildResult, tags=["clusters"])
def rebuild(db: Session = Depends(get_db)) -> RebuildResult:
    cluster_count, event_count = clustering.rebuild_all(db)
    return RebuildResult(cluster_count=cluster_count, event_count=event_count)
