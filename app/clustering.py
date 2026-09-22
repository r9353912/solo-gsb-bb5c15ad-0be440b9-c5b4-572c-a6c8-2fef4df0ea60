"""Event clustering core.

Merging rules
-------------
* **Spatial adjacency** – the globe is projected with an equirectangular
  projection and divided into square cells of ``GRID_CELL_METERS`` metres.
  Two events are spatial neighbours when they sit in the same cell or in any
  of the 8 surrounding cells (a 3x3 window).
* **Temporal adjacency** – two time intervals belong together when they
  overlap *or* the gap between them is at most
  ``GAP_TOLERANCE_SECONDS`` seconds.
* A new event joins every active cluster that has at least one member
  satisfying both rules. If several clusters match they are transitively
  merged (union-find style) into one surviving cluster.

Conflict resolution
--------------------
Members of one cluster whose intervals *strictly* overlap are treated as
duplicate observations of the same sound. The record with the highest
confidence wins (ties broken by earliest start, then earliest end, then
lowest id) and becomes the cluster representative; every losing record is
kept but logged with an ``event_conflict`` audit entry carrying the source
``device_id``. Absorbed clusters keep a ``merged_into_id`` pointer and their
original audit rows, so the full provenance chain can always be traversed.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .database import AuditEntry, Cluster, NoiseEvent

# A process-wide ingest lock keeps SQLite writes and the
# find-candidates -> merge -> reattach sequence race free for a single
# uvicorn worker (the documented deployment mode).
_ingest_lock = threading.RLock()

_EARTH_RADIUS_M = 6_371_000.0


# ---------------------------------------------------------------------------
# Geometry / time helpers
# ---------------------------------------------------------------------------

def grid_cell(lat: float, lon: float) -> tuple[int, int]:
    """Return the (x, y) cell indices for a coordinate."""
    lat_rad = math.radians(lat)
    m_per_deg_lat = math.pi * _EARTH_RADIUS_M / 180.0
    m_per_deg_lon = m_per_deg_lat * max(math.cos(lat_rad), 1e-9)
    x = int(math.floor((lon * m_per_deg_lon) / settings.grid_cell_meters))
    y = int(math.floor((lat * m_per_deg_lat) / settings.grid_cell_meters))
    return x, y


def neighboring_cells(cell_x: int, cell_y: int) -> list[tuple[int, int]]:
    """The 3x3 window of cells around (and including) a cell."""
    return [
        (cell_x + dx, cell_y + dy)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    ]


def intervals_adjacent(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
    tolerance: timedelta,
) -> bool:
    """True if intervals overlap or are separated by at most ``tolerance``."""
    if a_start <= b_end and b_start <= a_end:
        return True
    gap = max((b_start - a_end), (a_start - b_end))
    return gap <= tolerance


def intervals_strictly_overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """True for a positive-length overlap (touching endpoints do not count)."""
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _next_seq(db: Session, cluster_id: int) -> int:
    current = db.scalar(
        select(AuditEntry.seq)
        .where(AuditEntry.cluster_id == cluster_id)
        .order_by(AuditEntry.seq.desc())
        .limit(1)
    )
    return (current or 0) + 1


def _audit(
    db: Session,
    cluster_id: int,
    action: str,
    *,
    event_id: int | None = None,
    other_cluster_id: int | None = None,
    device_id: str | None = None,
    detail: str | None = None,
) -> AuditEntry:
    entry = AuditEntry(
        cluster_id=cluster_id,
        seq=_next_seq(db, cluster_id),
        action=action,
        event_id=event_id,
        other_cluster_id=other_cluster_id,
        device_id=device_id,
        detail=detail,
    )
    db.add(entry)
    db.flush()  # make the row visible to subsequent _next_seq lookups (autoflush disabled)
    return entry


# ---------------------------------------------------------------------------
# Cluster aggregation
# ---------------------------------------------------------------------------

def _event_key(event: NoiseEvent) -> tuple[float, datetime, datetime, int]:
    # Higher confidence first; earlier start/end then lower id break ties.
    return (-event.confidence, event.start_time, event.end_time, event.id)


def _recount_cluster(db: Session, cluster: Cluster) -> NoiseEvent | None:
    """Recompute aggregate fields of a cluster from its live members.

    Returns the winning representative event (highest-confidence overlapping
    member) or ``None`` for an empty cluster.
    """
    members = list(
        db.scalars(select(NoiseEvent).where(NoiseEvent.cluster_id == cluster.id))
    )
    if not members:
        cluster.event_count = 0
        cluster.device_ids = ""
        cluster.confidence = 0.0
        cluster.representative_event_id = None
        return None

    ordered = sorted(members, key=_event_key)
    winner = ordered[0]
    overlapping: list[NoiseEvent] = [winner]
    for candidate in ordered[1:]:
        if any(
            intervals_strictly_overlap(
                candidate.start_time, candidate.end_time, m.start_time, m.end_time
            )
            for m in overlapping
        ):
            overlapping.append(candidate)

    representative = min(overlapping, key=_event_key)

    cluster.event_count = len(members)
    cluster.device_ids = ",".join(
        sorted({m.device_id for m in members if m.device_id})
    )
    cluster.started_at = min(m.start_time for m in members)
    cluster.ended_at = max(m.end_time for m in members)
    cluster.confidence = representative.confidence
    cluster.representative_event_id = representative.id
    cell_xs = {m.cell_x for m in members}
    cell_ys = {m.cell_y for m in members}
    cluster.cell_x = min(cell_xs)
    cluster.cell_y = min(cell_ys)
    return representative


def _log_conflicts(
    db: Session, cluster: Cluster, members: Iterable[NoiseEvent]
) -> None:
    """Append ``event_conflict`` entries for overlapping losers.

    Idempotent: an (cluster_id, event_id, action) pair is logged only once.
    """
    members = list(members)
    by_start = sorted(members, key=lambda e: (e.start_time, e.end_time, e.id))
    already = {
        row[0]
        for row in db.execute(
            select(AuditEntry.event_id).where(
                AuditEntry.cluster_id == cluster.id,
                AuditEntry.action == "event_conflict",
            )
        ).all()
    }

    for i, loser in enumerate(by_start):
        for winner in by_start[:i] + by_start[i + 1 :]:
            if intervals_strictly_overlap(
                loser.start_time, loser.end_time, winner.start_time, winner.end_time
            ) and _event_key(winner) < _event_key(loser):
                if loser.id in already:
                    break
                _audit(
                    db,
                    cluster.id,
                    "event_conflict",
                    event_id=loser.id,
                    other_cluster_id=None,
                    device_id=loser.device_id,
                    detail=(
                        f"overlaps event_id={winner.id} from device_id={winner.device_id}; "
                        f"kept lower-confidence record confidence={loser.confidence:.3f} "
                        f"vs winner confidence={winner.confidence:.3f}"
                    ),
                )
                break


# ---------------------------------------------------------------------------
# Cluster absorption
# ---------------------------------------------------------------------------

def _absorb(db: Session, survivor: Cluster, victim: Cluster) -> None:
    """Merge ``victim`` into ``survivor`` (survivor has the winning key)."""
    members = db.scalars(
        select(NoiseEvent).where(NoiseEvent.cluster_id == victim.id)
    ).all()
    for member in members:
        member.cluster_id = survivor.id

    victim.active = False
    victim.merged_into_id = survivor.id

    _audit(
        db,
        victim.id,
        "cluster_merged",
        other_cluster_id=survivor.id,
        detail=f"cluster {victim.id} absorbed into cluster {survivor.id}",
    )
    _audit(
        db,
        survivor.id,
        "cluster_merged",
        other_cluster_id=victim.id,
        detail=f"cluster {survivor.id} absorbed cluster {victim.id} with {len(members)} event(s)",
    )


def _choose_survivor(
    db: Session, cluster_ids: Iterable[int]
) -> tuple[Cluster, list[Cluster]]:
    clusters = list(
        db.scalars(select(Cluster).where(Cluster.id.in_(list(cluster_ids))))
    )
    # Survivor = highest representative confidence, earliest start, lowest id.
    clusters.sort(key=lambda c: (-c.confidence, c.started_at, c.id))
    return clusters[0], clusters[1:]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _find_adjacent_clusters(db: Session, event: NoiseEvent) -> list[Cluster]:
    """Active clusters having at least one member spatially and temporally adjacent.

    Candidate membership is evaluated on *member* cells (not the cluster anchor
    cell), so clusters that have already grown across several grid cells are
    still found through any of their members. This keeps incremental ingest
    consistent with the union-find rebuild.
    """
    tolerance = timedelta(seconds=settings.gap_tolerance_seconds)
    window = neighboring_cells(event.cell_x, event.cell_y)
    cell_xs = {x for x, _ in window}
    cell_ys = {y for _, y in window}

    members_in_window = db.scalars(
        select(NoiseEvent)
        .join(Cluster, Cluster.id == NoiseEvent.cluster_id)
        .where(
            Cluster.active.is_(True),
            NoiseEvent.cell_x.in_(cell_xs),
            NoiseEvent.cell_y.in_(cell_ys),
        )
    ).all()

    adjacent_ids: set[int] = set()
    for member in members_in_window:
        if (
            member.cluster_id is not None
            and intervals_adjacent(
                event.start_time,
                event.end_time,
                member.start_time,
                member.end_time,
                tolerance,
            )
        ):
            adjacent_ids.add(member.cluster_id)

    if not adjacent_ids:
        return []
    return list(db.scalars(select(Cluster).where(Cluster.id.in_(adjacent_ids))))


def ingest_event(db: Session, event: NoiseEvent) -> tuple[NoiseEvent, list[int]]:
    """Attach a persisted-but-unassigned event to clusters, merging as needed."""
    with _ingest_lock:
        adjacent = _find_adjacent_clusters(db, event)
        merged_ids: list[int] = []

        if not adjacent:
            cluster = Cluster(
                cell_x=event.cell_x,
                cell_y=event.cell_y,
                started_at=event.start_time,
                ended_at=event.end_time,
                confidence=event.confidence,
                representative_event_id=None,
            )
            db.add(cluster)
            db.flush()
            event.cluster_id = cluster.id
            _audit(
                db,
                cluster.id,
                "cluster_created",
                event_id=event.id,
                device_id=event.device_id,
                detail=f"created from event {event.id} reported by {event.device_id}",
            )
            _audit(
                db,
                cluster.id,
                "event_added",
                event_id=event.id,
                device_id=event.device_id,
            )
            _recount_cluster(db, cluster)
            db.commit()
            return event, merged_ids

        survivor, victims = _choose_survivor(db, [c.id for c in adjacent])
        for victim in victims:
            _absorb(db, survivor, victim)
            merged_ids.append(victim.id)

        event.cluster_id = survivor.id
        _audit(
            db,
            survivor.id,
            "event_added",
            event_id=event.id,
            device_id=event.device_id,
        )
        db.flush()

        members = list(
            db.scalars(select(NoiseEvent).where(NoiseEvent.cluster_id == survivor.id))
        )
        _recount_cluster(db, survivor)
        _log_conflicts(db, survivor, members)
        db.commit()
        return event, merged_ids


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

class _DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def rebuild_all(db: Session) -> tuple[int, int]:
    """Recompute every cluster from the raw events (audit trail retained)."""
    with _ingest_lock:
        events = list(db.scalars(select(NoiseEvent).order_by(NoiseEvent.start_time)))
        db.execute(delete(AuditEntry))
        db.execute(delete(Cluster))
        db.flush()

        if not events:
            db.commit()
            return 0, 0

        dsu = _DSU(len(events))
        tolerance = timedelta(seconds=settings.gap_tolerance_seconds)
        index: dict[tuple[int, int], list[int]] = {}
        for i, event in enumerate(events):
            window = neighboring_cells(event.cell_x, event.cell_y)
            for cell in window:
                for j in index.get(cell, []):
                    other = events[j]
                    if intervals_adjacent(
                        event.start_time,
                        event.end_time,
                        other.start_time,
                        other.end_time,
                        tolerance,
                    ):
                        dsu.union(i, j)
            index.setdefault((event.cell_x, event.cell_y), []).append(i)

        groups: dict[int, list[int]] = {}
        for i in range(len(events)):
            groups.setdefault(dsu.find(i), []).append(i)

        cluster_count = 0
        for root, member_indexes in groups.items():
            members = [events[i] for i in member_indexes]
            seed = min(members, key=lambda m: (m.start_time, m.end_time, m.id))
            cluster = Cluster(
                cell_x=seed.cell_x,
                cell_y=seed.cell_y,
                started_at=min(m.start_time for m in members),
                ended_at=max(m.end_time for m in members),
                confidence=0.0,
            )
            db.add(cluster)
            db.flush()
            cluster_count += 1
            for member in members:
                member.cluster_id = cluster.id
            _recount_cluster(db, cluster)
            _audit(
                db,
                cluster.id,
                "cluster_rebuilt",
                event_id=cluster.representative_event_id,
                device_id=next(
                    (m.device_id for m in members if m.id == cluster.representative_event_id),
                    None,
                ),
                detail=f"rebuilt cluster with {len(members)} event(s)",
            )
            for member in sorted(members, key=lambda m: (m.start_time, m.end_time, m.id)):
                _audit(
                    db,
                    cluster.id,
                    "event_added",
                    event_id=member.id,
                    device_id=member.device_id,
                )
            _log_conflicts(db, cluster, members)

        db.commit()
        return cluster_count, len(events)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def make_event(payload: dict) -> NoiseEvent:
    start_raw: datetime = payload["start_time"]
    end_raw: datetime = payload["end_time"]
    # SQLite DateTime returns naive values; store naive UTC consistently so
    # comparisons in clustering never mix aware/naive datetimes.
    start_utc = start_raw.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_raw.astimezone(timezone.utc).replace(tzinfo=None)
    observed_utc: datetime | None = payload.get("observed_at")
    if observed_utc is None:
        observed_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        observed_utc = observed_utc.astimezone(timezone.utc).replace(tzinfo=None)

    cell_x, cell_y = grid_cell(payload["latitude"], payload["longitude"])
    return NoiseEvent(
        device_id=payload["device_id"],
        start_time=start_utc,
        end_time=end_utc,
        start_time_raw=start_raw.isoformat(),
        end_time_raw=end_raw.isoformat(),
        tz_offset_minutes=_raw_offset_minutes(start_raw),
        latitude=payload["latitude"],
        longitude=payload["longitude"],
        cell_x=cell_x,
        cell_y=cell_y,
        confidence=payload["confidence"],
        sound_type=payload.get("sound_type"),
        observed_at=observed_utc,
    )


def _raw_offset_minutes(value: datetime) -> int:
    offset = value.utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else 0
