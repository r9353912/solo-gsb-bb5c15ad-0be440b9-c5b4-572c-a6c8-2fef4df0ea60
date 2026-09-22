"""Geofence lifecycle management and enter/exit alert evaluation.

Alert semantics
---------------
* Every (geofence, device) pair runs a state machine over the device's
  events ordered by ``(start_time, id)``; the device starts ``outside``.
* An alert row is written **only** when the inside/outside state actually
  flips (``enter`` / ``exit``); repeated observations on the same side of
  the boundary change nothing.
* Events may arrive out of chronological order. When an event is not the
  newest observation of its (fence, device) pair, the whole timeline of the
  pair is recomputed from the stored events, so the alert stream always
  reflects event-time order regardless of arrival order.
* The point-in-polygon boundary rule is fixed (see :mod:`app.geometry`) and
  recorded on the geofence row and in the geofence audit trail.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .database import (
    Geofence,
    GeofenceAlert,
    GeofenceAudit,
    GeofenceDevice,
    GeofenceState,
    NoiseEvent,
)
from .geometry import BOUNDARY_RULE, BOUNDARY_RULE_DESCRIPTION, Polygon, normalize_ring

# Serializes state-machine updates against concurrent ingests/replays for a
# single uvicorn worker (the documented deployment mode), mirroring the
# ingest lock in app.clustering.
_geofence_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_polygon(fence: Geofence) -> Polygon:
    return Polygon(json.loads(fence.outer_json), json.loads(fence.holes_json))


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _next_seq(db: Session, fence_id: int) -> int:
    current = db.scalar(
        select(GeofenceAudit.seq)
        .where(GeofenceAudit.fence_id == fence_id)
        .order_by(GeofenceAudit.seq.desc())
        .limit(1)
    )
    return (current or 0) + 1


def _audit(
    db: Session,
    fence_id: int,
    action: str,
    *,
    device_id: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        GeofenceAudit(
            fence_id=fence_id,
            seq=_next_seq(db, fence_id),
            action=action,
            device_id=device_id,
            detail=detail,
        )
    )
    db.flush()  # make the row visible to subsequent _next_seq lookups


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_pair(db: Session, fence: Geofence, device_id: str) -> int:
    """Recompute the full enter/exit timeline of one (fence, device) pair.

    The device's events are walked in ``(start_time, id)`` order starting
    from the ``outside`` state; an alert row is written exactly when the
    inside/outside state flips. Existing alert rows of the pair are replaced
    by the recomputed timeline. Returns the number of alerts written.
    """
    events = list(
        db.scalars(
            select(NoiseEvent)
            .where(NoiseEvent.device_id == device_id)
            .order_by(NoiseEvent.start_time, NoiseEvent.id)
        )
    )
    polygon = load_polygon(fence)
    db.execute(
        delete(GeofenceAlert).where(
            GeofenceAlert.fence_id == fence.id,
            GeofenceAlert.device_id == device_id,
        )
    )
    inside = False
    alert_count = 0
    for event in events:
        now_inside = polygon.contains(event.longitude, event.latitude)
        if now_inside != inside:
            db.add(
                GeofenceAlert(
                    fence_id=fence.id,
                    device_id=device_id,
                    event_id=event.id,
                    transition="enter" if now_inside else "exit",
                    occurred_at=event.start_time,
                )
            )
            inside = now_inside
            alert_count += 1

    state = db.scalar(
        select(GeofenceState).where(
            GeofenceState.fence_id == fence.id,
            GeofenceState.device_id == device_id,
        )
    )
    if state is None:
        state = GeofenceState(fence_id=fence.id, device_id=device_id)
        db.add(state)
    state.inside = inside
    last = events[-1] if events else None
    state.last_event_id = last.id if last is not None else None
    state.last_occurred_at = last.start_time if last is not None else None
    state.updated_at = _now()
    db.flush()
    return alert_count


def process_event(db: Session, event: NoiseEvent) -> list[GeofenceAlert]:
    """Fold one freshly ingested event into the geofence state machines.

    Returns the alert rows whose transition was triggered by this event
    (usually empty, at most one per fence bound to the event's device).
    """
    with _geofence_lock:
        fence_ids = list(
            db.scalars(
                select(GeofenceDevice.fence_id).where(
                    GeofenceDevice.device_id == event.device_id
                )
            )
        )
        if not fence_ids:
            return []
        generated: list[GeofenceAlert] = []
        for fence_id in fence_ids:
            fence = db.get(Geofence, fence_id)
            if fence is None:
                continue
            state = db.scalar(
                select(GeofenceState).where(
                    GeofenceState.fence_id == fence_id,
                    GeofenceState.device_id == event.device_id,
                )
            )
            is_newest = (
                state is not None
                and state.last_event_id is not None
                and (event.start_time, event.id)
                > (state.last_occurred_at, state.last_event_id)
            )
            if is_newest:
                # Fast path: append-only, equivalent to a full recompute.
                inside = load_polygon(fence).contains(event.longitude, event.latitude)
                if inside != state.inside:
                    alert = GeofenceAlert(
                        fence_id=fence_id,
                        device_id=event.device_id,
                        event_id=event.id,
                        transition="enter" if inside else "exit",
                        occurred_at=event.start_time,
                    )
                    db.add(alert)
                    state.inside = inside
                    db.flush()
                    generated.append(alert)
                state.last_event_id = event.id
                state.last_occurred_at = event.start_time
                state.updated_at = _now()
                db.flush()
            else:
                # First observation or out-of-order arrival: rebuild the pair
                # timeline so alerts always follow event-time order.
                evaluate_pair(db, fence, event.device_id)
                generated.extend(
                    db.scalars(
                        select(GeofenceAlert).where(
                            GeofenceAlert.fence_id == fence_id,
                            GeofenceAlert.device_id == event.device_id,
                            GeofenceAlert.event_id == event.id,
                        )
                    )
                )
        db.commit()
        return generated


# ---------------------------------------------------------------------------
# Geofence lifecycle
# ---------------------------------------------------------------------------

def _set_bindings(db: Session, fence: Geofence, device_ids) -> list[str]:
    """Replace the fence's device bindings; prune alerts/state of removed devices."""
    wanted = list(dict.fromkeys(device_ids))
    existing = {
        row.device_id: row
        for row in db.scalars(
            select(GeofenceDevice).where(GeofenceDevice.fence_id == fence.id)
        )
    }
    for device_id in wanted:
        if device_id not in existing:
            db.add(GeofenceDevice(fence_id=fence.id, device_id=device_id))
            db.flush()
            _audit(
                db,
                fence.id,
                "device_bound",
                device_id=device_id,
                detail=f"device '{device_id}' bound to geofence {fence.id}",
            )
    for device_id, row in existing.items():
        if device_id not in wanted:
            db.delete(row)
            db.execute(
                delete(GeofenceAlert).where(
                    GeofenceAlert.fence_id == fence.id,
                    GeofenceAlert.device_id == device_id,
                )
            )
            db.execute(
                delete(GeofenceState).where(
                    GeofenceState.fence_id == fence.id,
                    GeofenceState.device_id == device_id,
                )
            )
            _audit(
                db,
                fence.id,
                "device_unbound",
                device_id=device_id,
                detail=(
                    f"device '{device_id}' unbound; its alerts for geofence "
                    f"{fence.id} were removed"
                ),
            )
    db.flush()
    return wanted


def create_fence(db: Session, *, name: str, outer, holes, device_ids) -> Geofence:
    with _geofence_lock:
        outer_ring = normalize_ring(outer)
        hole_rings = [normalize_ring(h) for h in holes]
        Polygon(outer_ring, hole_rings)  # topology validation (raises ValueError)
        fence = Geofence(
            name=name,
            outer_json=json.dumps([[lon, lat] for lon, lat in outer_ring]),
            holes_json=json.dumps(
                [[[lon, lat] for lon, lat in ring] for ring in hole_rings]
            ),
            boundary_rule=BOUNDARY_RULE,
        )
        db.add(fence)
        db.flush()
        _audit(
            db,
            fence.id,
            "geofence_created",
            detail=(
                f"geofence '{name}' created with {len(outer_ring)} outer vertices "
                f"and {len(hole_rings)} hole(s); boundary rule: "
                f"{BOUNDARY_RULE_DESCRIPTION}"
            ),
        )
        bound = _set_bindings(db, fence, device_ids)
        for device_id in bound:
            evaluate_pair(db, fence, device_id)
        db.commit()
        return fence


def update_fence(db: Session, fence: Geofence, *, name: str, outer, holes, device_ids) -> Geofence:
    with _geofence_lock:
        outer_ring = normalize_ring(outer)
        hole_rings = [normalize_ring(h) for h in holes]
        Polygon(outer_ring, hole_rings)
        fence.name = name
        fence.outer_json = json.dumps([[lon, lat] for lon, lat in outer_ring])
        fence.holes_json = json.dumps(
            [[[lon, lat] for lon, lat in ring] for ring in hole_rings]
        )
        fence.updated_at = _now()
        db.flush()
        _audit(
            db,
            fence.id,
            "geofence_updated",
            detail=(
                f"geofence updated: name='{name}', {len(outer_ring)} outer "
                f"vertices, {len(hole_rings)} hole(s); boundary rule unchanged: "
                f"{BOUNDARY_RULE_DESCRIPTION}"
            ),
        )
        bound = _set_bindings(db, fence, device_ids)
        for device_id in bound:
            evaluate_pair(db, fence, device_id)
        db.commit()
        return fence


def delete_fence(db: Session, fence: Geofence) -> None:
    with _geofence_lock:
        _audit(
            db,
            fence.id,
            "geofence_deleted",
            detail=(
                f"geofence '{fence.name}' deleted; bindings, state and alerts "
                f"removed; boundary rule was {BOUNDARY_RULE}"
            ),
        )
        db.delete(fence)  # ON DELETE CASCADE removes bindings/state/alerts
        db.commit()


def bind_device(db: Session, fence: Geofence, device_id: str) -> None:
    """Bind a device (idempotent) and evaluate its existing events."""
    with _geofence_lock:
        existing = db.scalar(
            select(GeofenceDevice).where(
                GeofenceDevice.fence_id == fence.id,
                GeofenceDevice.device_id == device_id,
            )
        )
        if existing is None:
            db.add(GeofenceDevice(fence_id=fence.id, device_id=device_id))
            db.flush()
            _audit(
                db,
                fence.id,
                "device_bound",
                device_id=device_id,
                detail=f"device '{device_id}' bound to geofence {fence.id}",
            )
            evaluate_pair(db, fence, device_id)
            db.commit()


def unbind_device(db: Session, fence: Geofence, device_id: str) -> bool:
    """Unbind a device, removing its alerts/state for this fence."""
    with _geofence_lock:
        binding = db.scalar(
            select(GeofenceDevice).where(
                GeofenceDevice.fence_id == fence.id,
                GeofenceDevice.device_id == device_id,
            )
        )
        if binding is None:
            return False
        db.delete(binding)
        db.execute(
            delete(GeofenceAlert).where(
                GeofenceAlert.fence_id == fence.id,
                GeofenceAlert.device_id == device_id,
            )
        )
        db.execute(
            delete(GeofenceState).where(
                GeofenceState.fence_id == fence.id,
                GeofenceState.device_id == device_id,
            )
        )
        _audit(
            db,
            fence.id,
            "device_unbound",
            device_id=device_id,
            detail=(
                f"device '{device_id}' unbound; its alerts for geofence "
                f"{fence.id} were removed"
            ),
        )
        db.commit()
        return True


def replay(db: Session, *, fence_id: int | None = None, device_id: str | None = None) -> dict:
    """Recompute alert timelines from the stored events.

    When replaying whole fences (no ``device_id`` filter), leftover
    alert/state rows for bindings that no longer exist are pruned first.
    """
    with _geofence_lock:
        stmt = select(Geofence).order_by(Geofence.id)
        if fence_id is not None:
            stmt = stmt.where(Geofence.id == fence_id)
        fences = list(db.scalars(stmt))
        pairs = 0
        for fence in fences:
            bstmt = select(GeofenceDevice).where(GeofenceDevice.fence_id == fence.id)
            if device_id is not None:
                bstmt = bstmt.where(GeofenceDevice.device_id == device_id)
            bindings = list(db.scalars(bstmt))
            if device_id is None:
                bound = {b.device_id for b in bindings}
                stale_alerts = delete(GeofenceAlert).where(
                    GeofenceAlert.fence_id == fence.id
                )
                stale_states = delete(GeofenceState).where(
                    GeofenceState.fence_id == fence.id
                )
                if bound:
                    stale_alerts = stale_alerts.where(
                        GeofenceAlert.device_id.not_in(bound)
                    )
                    stale_states = stale_states.where(
                        GeofenceState.device_id.not_in(bound)
                    )
                db.execute(stale_alerts)
                db.execute(stale_states)
            for binding in bindings:
                evaluate_pair(db, fence, binding.device_id)
                pairs += 1
            _audit(
                db,
                fence.id,
                "alerts_replayed",
                detail=(
                    f"replayed {len(bindings)} device binding(s); alert timeline "
                    f"recomputed from stored events; boundary rule: {BOUNDARY_RULE}"
                ),
            )
        db.commit()
        count_stmt = select(func.count(GeofenceAlert.id))
        if fence_id is not None:
            count_stmt = count_stmt.where(GeofenceAlert.fence_id == fence_id)
        if device_id is not None:
            count_stmt = count_stmt.where(GeofenceAlert.device_id == device_id)
        return {
            "fences_processed": len(fences),
            "pairs_evaluated": pairs,
            "alert_count": db.scalar(count_stmt) or 0,
        }
