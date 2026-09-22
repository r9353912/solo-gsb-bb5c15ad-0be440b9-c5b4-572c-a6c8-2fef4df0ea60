"""Geofence lifecycle, device bindings and enter/exit alert evaluation.

State machine
-------------
When an event for device ``D`` arrives at (lat, lon, time) it is tested against
every **active** geofence currently bound to ``D``. Per (device, fence) the
device's previous state is the containment result for its chronologically
previous event (ordered by ``start_time`` then event id); with no prior
observation the device is treated as *outside*. An ``enter``/``exit`` alert is
written **only when the state actually changes**.

Replay
------
``replay_fence`` recomputes a fence's alerts from the full stored event
history of its currently bound devices in chronological order, replacing the
existing alert set. The fixed boundary rule (see :mod:`app.geometry`) is
recorded with the fence, every alert and every audit row.
"""
from __future__ import annotations

import base64
import json
import threading
from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from . import geometry
from .clustering import _ingest_lock
from .database import (
    Geofence,
    GeofenceAlert,
    GeofenceAuditEntry,
    GeofenceDevice,
    NoiseEvent,
)
from .geometry import BOUNDARY_RULE, Polygon

# ---------------------------------------------------------------------------
# Polygon (de)serialization & validation
# ---------------------------------------------------------------------------

def polygon_to_storage(polygon: Polygon) -> str:
    return json.dumps(
        {
            "outer": [[lat, lon] for lat, lon in polygon["outer"]],
            "holes": [[[lat, lon] for lat, lon in hole] for hole in polygon.get("holes", [])],
        },
        separators=(",", ":"),
    )


def polygon_from_payload(data) -> Polygon:
    """Convert a ``PolygonIn`` pydantic model into a geometry polygon."""
    polygon: Polygon = {
        "outer": [(p.latitude, p.longitude) for p in data.outer],
        "holes": [
            [(p.latitude, p.longitude) for p in hole] for hole in (data.holes or [])
        ],
    }
    geometry.validate_polygon(polygon)
    return polygon


def polygon_from_storage(raw: str) -> Polygon:
    data = json.loads(raw)
    return {
        "outer": [(lat, lon) for lat, lon in data["outer"]],
        "holes": [[(lat, lon) for lat, lon in hole] for hole in data.get("holes", [])],
    }


def crosses_antimeridian(polygon: Polygon) -> bool:
    if geometry.ring_crosses_antimeridian(polygon["outer"]):
        return True
    return any(geometry.ring_crosses_antimeridian(hole) for hole in polygon.get("holes", []))


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _next_audit_seq(db: Session, geofence_id: int) -> int:
    current = db.scalar(
        select(GeofenceAuditEntry.seq)
        .where(GeofenceAuditEntry.geofence_id == geofence_id)
        .order_by(GeofenceAuditEntry.seq.desc())
        .limit(1)
    )
    return (current or 0) + 1


def _fence_audit(
    db: Session,
    geofence_id: int,
    action: str,
    *,
    event_id: int | None = None,
    alert_id: int | None = None,
    device_id: str | None = None,
    detail: str | None = None,
) -> GeofenceAuditEntry:
    entry = GeofenceAuditEntry(
        geofence_id=geofence_id,
        seq=_next_audit_seq(db, geofence_id),
        action=action,
        event_id=event_id,
        alert_id=alert_id,
        device_id=device_id,
        detail=detail,
    )
    db.add(entry)
    db.flush()
    return entry


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_fence(db: Session, *, name: str, description: str | None, polygon: Polygon) -> Geofence:
    with _ingest_lock:
        existing = db.scalar(select(Geofence.id).where(Geofence.name == name))
        if existing is not None:
            raise ValueError(f"geofence name {name!r} already exists")
        fence = Geofence(
            name=name,
            description=description,
            rings_json=polygon_to_storage(polygon),
            boundary_rule=BOUNDARY_RULE,
            crosses_antimeridian=crosses_antimeridian(polygon),
            active=True,
        )
        db.add(fence)
        db.flush()
        _fence_audit(
            db,
            fence.id,
            "fence_created",
            detail=(
                f"boundary_rule={BOUNDARY_RULE} (outer-ring boundary inside, hole boundaries "
                f"outside); outer_vertices={len(polygon['outer']) - 1}; "
                f"holes={len(polygon.get('holes', []))}; "
                f"crosses_antimeridian={fence.crosses_antimeridian}"
            ),
        )
        db.commit()
        return fence


def update_fence(
    db: Session,
    fence: Geofence,
    *,
    name: str | None,
    description: str | None,
    polygon: Polygon | None,
) -> Geofence:
    with _ingest_lock:
        changes: list[str] = []
        if name is not None and name != fence.name:
            clash = db.scalar(
                select(Geofence.id).where(Geofence.name == name, Geofence.id != fence.id)
            )
            if clash is not None:
                raise ValueError(f"geofence name {name!r} already exists")
            changes.append(f"name {fence.name!r} -> {name!r}")
            fence.name = name
        if description is not None and description != fence.description:
            changes.append("description updated")
            fence.description = description
        if polygon is not None:
            fence.rings_json = polygon_to_storage(polygon)
            fence.crosses_antimeridian = crosses_antimeridian(polygon)
            changes.append(
                f"polygon replaced (outer_vertices={len(polygon['outer']) - 1}, "
                f"holes={len(polygon.get('holes', []))})"
            )
        if changes:
            _fence_audit(
                db,
                fence.id,
                "fence_updated",
                detail="; ".join(changes) + f"; boundary_rule={BOUNDARY_RULE}",
            )
            db.commit()
        return fence


def delete_fence(db: Session, fence: Geofence) -> None:
    with _ingest_lock:
        if not fence.active:
            return
        fence.active = False
        # Count bindings for the audit detail before detaching them.
        count = len(
            db.scalars(
                select(GeofenceDevice).where(GeofenceDevice.geofence_id == fence.id)
            ).all()
        )
        db.execute(delete(GeofenceDevice).where(GeofenceDevice.geofence_id == fence.id))
        _fence_audit(
            db,
            fence.id,
            "fence_deleted",
            detail=f"soft delete; {count} device binding(s) detached; alerts retained",
        )
        db.commit()


def list_bound_devices(db: Session, geofence_id: int) -> list[str]:
    rows = db.scalars(
        select(GeofenceDevice.device_id)
        .where(GeofenceDevice.geofence_id == geofence_id)
        .order_by(GeofenceDevice.device_id)
    ).all()
    return list(rows)


def set_bindings(db: Session, fence: Geofence, device_ids: Iterable[str]) -> list[str]:
    """Replace the full device binding set of a fence."""
    with _ingest_lock:
        wanted = sorted(set(device_ids))
        current = set(list_bound_devices(db, fence.id))
        added = [d for d in wanted if d not in current]
        removed = sorted(current - set(wanted))
        if added:
            db.add_all(
                GeofenceDevice(geofence_id=fence.id, device_id=d) for d in added
            )
        if removed:
            db.execute(
                delete(GeofenceDevice).where(
                    GeofenceDevice.geofence_id == fence.id,
                    GeofenceDevice.device_id.in_(removed),
                )
            )
        if added or removed:
            _fence_audit(
                db,
                fence.id,
                "devices_bound" if added else "devices_unbound",
                detail=(
                    f"replaced binding set; bound={added or 'none'}; "
                    f"unbound={removed or 'none'}"
                ),
            )
        db.commit()
        return wanted


def add_bindings(db: Session, fence: Geofence, device_ids: Iterable[str]) -> list[str]:
    with _ingest_lock:
        wanted = set(device_ids)
        current = set(list_bound_devices(db, fence.id))
        added = sorted(wanted - current)
        if added:
            db.add_all(
                GeofenceDevice(geofence_id=fence.id, device_id=d) for d in added
            )
            _fence_audit(
                db,
                fence.id,
                "devices_bound",
                detail=f"added {len(added)} device(s): {', '.join(added)}",
            )
            db.commit()
        return sorted(current | set(added))


def remove_bindings(db: Session, fence: Geofence, device_ids: Iterable[str]) -> list[str]:
    with _ingest_lock:
        wanted = set(device_ids)
        current = set(list_bound_devices(db, fence.id))
        removed = sorted(wanted & current)
        if removed:
            db.execute(
                delete(GeofenceDevice).where(
                    GeofenceDevice.geofence_id == fence.id,
                    GeofenceDevice.device_id.in_(removed),
                )
            )
            _fence_audit(
                db,
                fence.id,
                "devices_unbound",
                detail=f"removed {len(removed)} device(s): {', '.join(removed)}",
            )
            db.commit()
        return sorted(current - set(removed))


# ---------------------------------------------------------------------------
# Alert evaluation
# ---------------------------------------------------------------------------

def _previous_event(db: Session, device_id: str, event: NoiseEvent) -> NoiseEvent | None:
    return db.scalars(
        select(NoiseEvent)
        .where(
            NoiseEvent.device_id == device_id,
            or_(
                NoiseEvent.start_time < event.start_time,
                and_(
                    NoiseEvent.start_time == event.start_time,
                    NoiseEvent.id < event.id,
                ),
            ),
        )
        .order_by(NoiseEvent.start_time.desc(), NoiseEvent.id.desc())
        .limit(1)
    ).first()


def _active_fences_for_device(db: Session, device_id: str) -> list[Geofence]:
    return list(
        db.scalars(
            select(Geofence)
            .join(GeofenceDevice, GeofenceDevice.geofence_id == Geofence.id)
            .where(GeofenceDevice.device_id == device_id, Geofence.active.is_(True))
        )
    )


def _write_alert(
    db: Session,
    fence: Geofence,
    event: NoiseEvent,
    transition: str,
    *,
    replayed: bool = False,
) -> GeofenceAlert:
    alert = GeofenceAlert(
        geofence_id=fence.id,
        geofence_name=fence.name,
        device_id=event.device_id,
        transition=transition,
        occurred_at=event.start_time,
        latitude=event.latitude,
        longitude=event.longitude,
        event_id=event.id,
        boundary_rule=fence.boundary_rule,
        replayed=replayed,
    )
    db.add(alert)
    db.flush()
    _fence_audit(
        db,
        fence.id,
        "alert_triggered",
        event_id=event.id,
        alert_id=alert.id,
        device_id=event.device_id,
        detail=(
            f"{transition} at {event.latitude:.6f},{event.longitude:.6f} "
            f"observed at {event.start_time.isoformat()}Z; boundary_rule={fence.boundary_rule}"
        ),
    )
    return alert


def evaluate_event(db: Session, event: NoiseEvent) -> list[GeofenceAlert]:
    """Test a freshly stored event against the device's active geofences.

    Alerts are written only when containment state changes relative to the
    device's chronologically previous observation (baseline: outside).
    """
    alerts: list[GeofenceAlert] = []
    with _ingest_lock:
        fences = _active_fences_for_device(db, event.device_id)
        if not fences:
            return alerts
        previous = _previous_event(db, event.device_id, event)
        for fence in fences:
            polygon = polygon_from_storage(fence.rings_json)
            inside = geometry.polygon_contains(event.latitude, event.longitude, polygon)
            if previous is None:
                prev_inside = False
            else:
                prev_inside = geometry.polygon_contains(
                    previous.latitude, previous.longitude, polygon
                )
            if inside == prev_inside:
                continue
            alerts.append(_write_alert(db, fence, event, "enter" if inside else "exit"))
        if alerts:
            db.commit()
        return alerts


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def replay_fence(db: Session, fence: Geofence) -> tuple[list[int], int]:
    """Recompute all alerts of a fence from stored events, in time order.

    Uses the fence's current device bindings and current polygon/boundary
    rule. Previous alert rows (and their ``alert_triggered`` audit rows) are
    replaced; a single ``alerts_replayed`` summary audit row is appended.
    """
    with _ingest_lock:
        devices = list_bound_devices(db, fence.id)
        old_ids = list(
            db.scalars(select(GeofenceAlert.id).where(GeofenceAlert.geofence_id == fence.id))
        )
        db.execute(delete(GeofenceAlert).where(GeofenceAlert.geofence_id == fence.id))
        db.execute(
            delete(GeofenceAuditEntry).where(
                GeofenceAuditEntry.geofence_id == fence.id,
                GeofenceAuditEntry.action == "alert_triggered",
            )
        )
        db.flush()

        created = 0
        if devices:
            polygon = polygon_from_storage(fence.rings_json)
            # Per-device state walked in global chronological order.
            states: dict[str, bool] = {device: False for device in devices}
            events = db.scalars(
                select(NoiseEvent)
                .where(NoiseEvent.device_id.in_(devices))
                .order_by(NoiseEvent.start_time, NoiseEvent.id)
            ).all()
            for event in events:
                inside = geometry.polygon_contains(
                    event.latitude, event.longitude, polygon
                )
                if inside != states[event.device_id]:
                    _write_alert(db, fence, event, "enter" if inside else "exit", replayed=True)
                    states[event.device_id] = inside
                    created += 1

        _fence_audit(
            db,
            fence.id,
            "alerts_replayed",
            detail=(
                f"recomputed from event history for {len(devices)} bound device(s); "
                f"removed {len(old_ids)} prior alert(s); created {created} alert(s); "
                f"boundary_rule={fence.boundary_rule}"
            ),
        )
        db.commit()
        return sorted(old_ids), created


# ---------------------------------------------------------------------------
# Keyset (time) pagination for alerts
# ---------------------------------------------------------------------------

def encode_cursor(occurred_at: datetime, alert_id: int) -> str:
    raw = json.dumps(
        {"t": occurred_at.replace(tzinfo=None).isoformat(), "id": alert_id}
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(data["t"]), int(data["id"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid cursor") from exc


def query_alerts(
    db: Session,
    *,
    geofence_id: int | None = None,
    device_id: str | None = None,
    transition: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    cursor: tuple[datetime, int] | None = None,
    limit: int,
) -> list[GeofenceAlert]:
    stmt = select(GeofenceAlert)
    if geofence_id is not None:
        stmt = stmt.where(GeofenceAlert.geofence_id == geofence_id)
    if device_id is not None:
        stmt = stmt.where(GeofenceAlert.device_id == device_id)
    if transition is not None:
        stmt = stmt.where(GeofenceAlert.transition == transition)
    if occurred_after is not None:
        stmt = stmt.where(GeofenceAlert.occurred_at >= occurred_after)
    if occurred_before is not None:
        stmt = stmt.where(GeofenceAlert.occurred_at < occurred_before)
    if cursor is not None:
        cursor_time, cursor_id = cursor
        stmt = stmt.where(
            or_(
                GeofenceAlert.occurred_at > cursor_time,
                and_(
                    GeofenceAlert.occurred_at == cursor_time,
                    GeofenceAlert.id > cursor_id,
                ),
            )
        )
    stmt = stmt.order_by(GeofenceAlert.occurred_at, GeofenceAlert.id).limit(limit + 1)
    return list(db.scalars(stmt).all())
