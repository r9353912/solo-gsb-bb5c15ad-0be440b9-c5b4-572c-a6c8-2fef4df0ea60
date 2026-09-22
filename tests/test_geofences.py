"""End-to-end tests for geofence CRUD, enter/exit alerts and replay."""
from __future__ import annotations

# A 0.01° x 0.01° square around (121.475, 31.235); positions are [lon, lat].
OUTER = [[121.47, 31.23], [121.48, 31.23], [121.48, 31.24], [121.47, 31.24]]
INSIDE = {"lat": 31.235, "lon": 121.475}
OUTSIDE = {"lat": 31.25, "lon": 121.49}


def _fence(name="yard", outer=None, holes=None, device_ids=None):
    return {
        "name": name,
        "outer": outer if outer is not None else OUTER,
        "holes": holes if holes is not None else [],
        "device_ids": device_ids if device_ids is not None else [],
    }


def _event(
    device_id="dev-1",
    start="2026-09-22T08:00:00+08:00",
    end="2026-09-22T08:00:30+08:00",
    lat=INSIDE["lat"],
    lon=INSIDE["lon"],
    confidence=0.8,
):
    return {
        "device_id": device_id,
        "start_time": start,
        "end_time": end,
        "latitude": lat,
        "longitude": lon,
        "confidence": confidence,
        "sound_type": "traffic",
    }


def _create_fence(client, **kwargs):
    r = client.post("/api/v1/geofences", json=_fence(**kwargs))
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# CRUD & validation
# ---------------------------------------------------------------------------

def test_geofence_crud_and_audit(client):
    body = _create_fence(client, device_ids=["dev-1"])
    assert body["id"] == 1
    assert body["name"] == "yard"
    assert body["outer"] == OUTER
    assert body["holes"] == []
    assert body["boundary_rule"] == "boundary_inclusive"
    assert body["device_ids"] == ["dev-1"]
    assert body["alert_count"] == 0

    assert client.get("/api/v1/geofences/1").json()["name"] == "yard"
    assert len(client.get("/api/v1/geofences").json()) == 1
    assert len(client.get("/api/v1/geofences", params={"device_id": "dev-1"}).json()) == 1
    assert client.get("/api/v1/geofences", params={"device_id": "other"}).json() == []

    # Full replacement: new name, shifted geometry, new device set.
    shifted = [[lon + 1.0, lat] for lon, lat in OUTER]
    r = client.put(
        "/api/v1/geofences/1",
        json=_fence(name="yard-east", outer=shifted, device_ids=["dev-2"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "yard-east"
    assert body["outer"] == shifted
    assert body["device_ids"] == ["dev-2"]

    audit = client.get("/api/v1/geofences/1/audit").json()
    actions = [a["action"] for a in audit]
    assert actions == [
        "geofence_created",
        "device_bound",
        "geofence_updated",
        "device_bound",
        "device_unbound",
    ]
    assert [a["seq"] for a in audit] == [1, 2, 3, 4, 5]
    # The fixed boundary rule is recorded in the audit trail.
    assert "boundary_inclusive" in audit[0]["detail"]
    assert "boundary rule" in audit[2]["detail"]  # geofence_updated entry
    assert audit[1]["device_id"] == "dev-1"
    assert audit[4]["device_id"] == "dev-1"

    assert client.delete("/api/v1/geofences/1").status_code == 204
    assert client.get("/api/v1/geofences/1").status_code == 404
    # The audit trail survives deletion and records the delete itself.
    audit = client.get("/api/v1/geofences/1/audit").json()
    assert audit[-1]["action"] == "geofence_deleted"


def test_geofence_404s(client):
    assert client.get("/api/v1/geofences/999").status_code == 404
    assert client.put("/api/v1/geofences/999", json=_fence()).status_code == 404
    assert client.delete("/api/v1/geofences/999").status_code == 404
    assert (
        client.post("/api/v1/geofences/999/devices", json={"device_id": "d"}).status_code
        == 404
    )
    assert client.delete("/api/v1/geofences/999/devices/d").status_code == 404
    assert client.get("/api/v1/geofences/999/audit").status_code == 404
    assert client.post("/api/v1/geofences/replay", params={"fence_id": 999}).status_code == 404


def test_geofence_validation(client):
    # Fewer than 3 outer vertices.
    assert client.post(
        "/api/v1/geofences", json=_fence(outer=[[121.47, 31.23], [121.48, 31.23]])
    ).status_code == 422
    # Longitude / latitude out of range.
    bad_lon = [[181.0, 31.23], [121.48, 31.23], [121.48, 31.24]]
    assert client.post("/api/v1/geofences", json=_fence(outer=bad_lon)).status_code == 422
    bad_lat = [[121.47, 91.0], [121.48, 31.23], [121.48, 31.24]]
    assert client.post("/api/v1/geofences", json=_fence(outer=bad_lat)).status_code == 422
    # Malformed position (not a pair).
    assert client.post(
        "/api/v1/geofences",
        json=_fence(outer=[[121.47], [121.48, 31.23], [121.48, 31.24]]),
    ).status_code == 422
    # Hole with fewer than 3 vertices.
    assert client.post(
        "/api/v1/geofences", json=_fence(holes=[[[121.472, 31.232], [121.473, 31.232]]])
    ).status_code == 422
    # Ring winding more than 360° of longitude.
    winding = [[0, 0], [170, 0], [-170, 0], [10, 0], [180, 0]]
    assert client.post("/api/v1/geofences", json=_fence(outer=winding)).status_code == 422


def test_closed_ring_is_unclosed_on_store(client):
    closed = OUTER + [OUTER[0]]
    body = _create_fence(client, outer=closed)
    assert body["outer"] == OUTER


# ---------------------------------------------------------------------------
# Enter/exit state machine
# ---------------------------------------------------------------------------

def test_enter_exit_alerts_only_on_transition(client):
    _create_fence(client, device_ids=["dev-1"])

    # First observation inside -> enter alert, echoed in the ingest response.
    r = client.post("/api/v1/events", json=_event())
    alerts = r.json()["geofence_alerts"]
    assert len(alerts) == 1
    assert alerts[0]["transition"] == "enter"
    assert alerts[0]["fence_id"] == 1
    assert alerts[0]["fence_name"] == "yard"
    assert alerts[0]["device_id"] == "dev-1"
    assert alerts[0]["occurred_at"] == "2026-09-22T00:00:00Z"

    # Still inside -> no new alert.
    r = client.post(
        "/api/v1/events",
        json=_event(start="2026-09-22T08:01:00+08:00", end="2026-09-22T08:01:30+08:00"),
    )
    assert r.json()["geofence_alerts"] == []

    # Moving outside -> exit alert.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:02:00+08:00",
            end="2026-09-22T08:02:30+08:00",
            **OUTSIDE,
        ),
    )
    alerts = r.json()["geofence_alerts"]
    assert len(alerts) == 1
    assert alerts[0]["transition"] == "exit"
    assert alerts[0]["occurred_at"] == "2026-09-22T00:02:00Z"

    # Still outside -> nothing.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:03:00+08:00",
            end="2026-09-22T08:03:30+08:00",
            **OUTSIDE,
        ),
    )
    assert r.json()["geofence_alerts"] == []

    lst = client.get("/api/v1/geofence-alerts", params={"fence_id": 1}).json()
    assert [a["transition"] for a in lst] == ["enter", "exit"]
    assert client.get("/api/v1/geofences/1").json()["alert_count"] == 2


def test_first_observation_outside_creates_no_alert(client):
    _create_fence(client, device_ids=["dev-1"])
    r = client.post("/api/v1/events", json=_event(**OUTSIDE))
    assert r.json()["geofence_alerts"] == []
    assert client.get("/api/v1/geofence-alerts").json() == []


def test_unbound_device_creates_no_alert(client):
    _create_fence(client, device_ids=["dev-1"])
    r = client.post("/api/v1/events", json=_event(device_id="dev-9"))
    assert r.json()["geofence_alerts"] == []
    assert client.get("/api/v1/geofence-alerts").json() == []


def test_out_of_order_events_recompute_timeline(client):
    _create_fence(client, device_ids=["dev-1"])

    # Newest first: enter at 08:10.
    client.post(
        "/api/v1/events",
        json=_event(start="2026-09-22T08:10:00+08:00", end="2026-09-22T08:10:30+08:00"),
    )
    # An earlier inside observation arrives late: the enter moves to 08:00.
    client.post(
        "/api/v1/events",
        json=_event(start="2026-09-22T08:00:00+08:00", end="2026-09-22T08:00:30+08:00"),
    )
    lst = client.get("/api/v1/geofence-alerts").json()
    assert [(a["transition"], a["occurred_at"]) for a in lst] == [
        ("enter", "2026-09-22T00:00:00Z")
    ]

    # An out-of-order outside observation in between splits the stay.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:05:00+08:00",
            end="2026-09-22T08:05:30+08:00",
            **OUTSIDE,
        ),
    )
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["exit"]
    lst = client.get("/api/v1/geofence-alerts").json()
    assert [(a["transition"], a["occurred_at"]) for a in lst] == [
        ("enter", "2026-09-22T00:00:00Z"),
        ("exit", "2026-09-22T00:05:00Z"),
        ("enter", "2026-09-22T00:10:00Z"),
    ]


# ---------------------------------------------------------------------------
# Boundary rule, holes, antimeridian
# ---------------------------------------------------------------------------

def test_boundary_points_count_as_inside(client):
    _create_fence(client, device_ids=["dev-1"])
    # Exactly on the bottom edge of the fence.
    r = client.post("/api/v1/events", json=_event(lat=31.23, lon=121.475))
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["enter"]
    # Exactly on a vertex: still inside, no new transition.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:01:00+08:00",
            end="2026-09-22T08:01:30+08:00",
            lat=31.23,
            lon=121.47,
        ),
    )
    assert r.json()["geofence_alerts"] == []


def test_hole_excludes_interior_but_not_its_edge(client):
    hole = [[121.472, 31.232], [121.474, 31.232], [121.474, 31.234], [121.472, 31.234]]
    _create_fence(client, holes=[hole], device_ids=["dev-1"])

    # Inside the hole -> outside the fence, no alert.
    r = client.post("/api/v1/events", json=_event(lat=31.233, lon=121.473))
    assert r.json()["geofence_alerts"] == []

    # Between outer ring and hole -> inside.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:01:00+08:00",
            end="2026-09-22T08:01:30+08:00",
            lat=31.231,
            lon=121.473,
        ),
    )
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["enter"]

    # Exactly on the hole edge -> boundary-inclusive -> still inside.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:02:00+08:00",
            end="2026-09-22T08:02:30+08:00",
            lat=31.233,
            lon=121.472,
        ),
    )
    assert r.json()["geofence_alerts"] == []

    # Back into the hole -> exit.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:03:00+08:00",
            end="2026-09-22T08:03:30+08:00",
            lat=31.233,
            lon=121.473,
        ),
    )
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["exit"]


def test_antimeridian_crossing_fence(client):
    # A band across the Pacific: longitudes 170° .. -170° (through ±180°).
    outer = [[170.0, 40.0], [-170.0, 40.0], [-170.0, 50.0], [170.0, 50.0]]
    _create_fence(client, outer=outer, device_ids=["dev-1"])

    r = client.post("/api/v1/events", json=_event(lat=45.0, lon=179.5))
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["enter"]

    # The same physical band expressed with a negative longitude.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:01:00+08:00",
            end="2026-09-22T08:01:30+08:00",
            lat=45.0,
            lon=-179.5,
        ),
    )
    assert r.json()["geofence_alerts"] == []

    # lon=0 is far outside the band -> exit.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:02:00+08:00",
            end="2026-09-22T08:02:30+08:00",
            lat=45.0,
            lon=0.0,
        ),
    )
    assert [a["transition"] for a in r.json()["geofence_alerts"]] == ["exit"]

    # North of the band: still outside, no alert.
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:03:00+08:00",
            end="2026-09-22T08:03:30+08:00",
            lat=55.0,
            lon=175.0,
        ),
    )
    assert r.json()["geofence_alerts"] == []


# ---------------------------------------------------------------------------
# Bindings, updates, replay
# ---------------------------------------------------------------------------

def test_bind_and_unbind_device(client):
    _create_fence(client)
    # Events exist before the device is bound.
    client.post("/api/v1/events", json=_event())
    assert client.get("/api/v1/geofence-alerts").json() == []

    # Binding evaluates the already-stored events of the device.
    r = client.post("/api/v1/geofences/1/devices", json={"device_id": "dev-1"})
    assert r.status_code == 200
    assert r.json()["device_ids"] == ["dev-1"]
    lst = client.get("/api/v1/geofence-alerts").json()
    assert [a["transition"] for a in lst] == ["enter"]

    # Binding again is idempotent.
    client.post("/api/v1/geofences/1/devices", json={"device_id": "dev-1"})
    assert len(client.get("/api/v1/geofence-alerts").json()) == 1

    # Unbinding removes the device's alerts for this fence.
    r = client.delete("/api/v1/geofences/1/devices/dev-1")
    assert r.status_code == 200
    assert r.json()["device_ids"] == []
    assert client.get("/api/v1/geofence-alerts").json() == []
    assert client.delete("/api/v1/geofences/1/devices/dev-1").status_code == 404

    # Rebinding recomputes them from the stored events.
    client.post("/api/v1/geofences/1/devices", json={"device_id": "dev-1"})
    assert len(client.get("/api/v1/geofence-alerts").json()) == 1


def test_geometry_update_reevaluates_bound_devices(client):
    _create_fence(client, device_ids=["dev-1"])
    client.post("/api/v1/events", json=_event())
    assert client.get("/api/v1/geofences/1").json()["alert_count"] == 1

    # Move the fence 1° east: the stored event is now outside -> no alerts.
    shifted = [[lon + 1.0, lat] for lon, lat in OUTER]
    r = client.put(
        "/api/v1/geofences/1",
        json=_fence(outer=shifted, device_ids=["dev-1"]),
    )
    assert r.status_code == 200
    assert r.json()["alert_count"] == 0
    assert client.get("/api/v1/geofence-alerts").json() == []

    # Move it back: the enter alert reappears.
    client.put("/api/v1/geofences/1", json=_fence(device_ids=["dev-1"]))
    lst = client.get("/api/v1/geofence-alerts").json()
    assert [a["transition"] for a in lst] == ["enter"]


def test_delete_fence_removes_alerts(client):
    _create_fence(client, device_ids=["dev-1"])
    client.post("/api/v1/events", json=_event())
    assert len(client.get("/api/v1/geofence-alerts").json()) == 1
    client.delete("/api/v1/geofences/1")
    assert client.get("/api/v1/geofence-alerts").json() == []


def test_replay_recomputes_and_is_idempotent(client):
    _create_fence(client, device_ids=["dev-1", "dev-2"])
    client.post("/api/v1/events", json=_event(device_id="dev-1"))
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="dev-1",
            start="2026-09-22T08:05:00+08:00",
            end="2026-09-22T08:05:30+08:00",
            **OUTSIDE,
        ),
    )
    client.post("/api/v1/events", json=_event(device_id="dev-2"))

    r = client.post("/api/v1/geofences/replay")
    assert r.status_code == 200
    assert r.json() == {"fences_processed": 1, "pairs_evaluated": 2, "alert_count": 3}
    # Idempotent: a second replay yields the same timeline.
    assert client.post("/api/v1/geofences/replay").json()["alert_count"] == 3
    lst = client.get("/api/v1/geofence-alerts").json()
    assert [(a["device_id"], a["transition"]) for a in lst] == [
        ("dev-1", "enter"),
        ("dev-2", "enter"),
        ("dev-1", "exit"),
    ]

    # Scoped replay of a single fence / device.
    r = client.post("/api/v1/geofences/replay", params={"fence_id": 1, "device_id": "dev-1"})
    assert r.json() == {"fences_processed": 1, "pairs_evaluated": 1, "alert_count": 2}

    audit = client.get("/api/v1/geofences/1/audit").json()
    assert "alerts_replayed" in [a["action"] for a in audit]


# ---------------------------------------------------------------------------
# Alert queries
# ---------------------------------------------------------------------------

def _two_fence_setup(client):
    _create_fence(client, name="f1", device_ids=["dev-1"])
    _create_fence(client, name="f2", device_ids=["dev-2"])
    client.post("/api/v1/events", json=_event(device_id="dev-1"))  # enter f1 @00:00
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="dev-2",
            start="2026-09-22T08:02:00+08:00",
            end="2026-09-22T08:02:30+08:00",
        ),
    )  # enter f2 @00:02
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="dev-1",
            start="2026-09-22T08:05:00+08:00",
            end="2026-09-22T08:05:30+08:00",
            **OUTSIDE,
        ),
    )  # exit f1 @00:05


def test_alert_pagination_and_filters(client):
    _two_fence_setup(client)

    lst = client.get("/api/v1/geofence-alerts").json()
    assert [(a["fence_name"], a["transition"]) for a in lst] == [
        ("f1", "enter"),
        ("f2", "enter"),
        ("f1", "exit"),
    ]

    assert len(client.get("/api/v1/geofence-alerts", params={"fence_id": 2}).json()) == 1
    assert len(client.get("/api/v1/geofence-alerts", params={"device_id": "dev-1"}).json()) == 2
    only_exits = client.get("/api/v1/geofence-alerts", params={"transition": "exit"}).json()
    assert [a["transition"] for a in only_exits] == ["exit"]

    windowed = client.get(
        "/api/v1/geofence-alerts",
        params={
            "occurred_after": "2026-09-22T00:01:00Z",
            "occurred_before": "2026-09-22T00:04:00Z",
        },
    ).json()
    assert [(a["fence_name"], a["transition"]) for a in windowed] == [("f2", "enter")]

    page = client.get("/api/v1/geofence-alerts", params={"limit": 1, "offset": 1}).json()
    assert len(page) == 1
    assert page[0]["fence_name"] == "f2"

    # Naive (timezone-less) window bounds are rejected.
    assert (
        client.get(
            "/api/v1/geofence-alerts", params={"occurred_after": "2026-09-22T00:01:00"}
        ).status_code
        == 422
    )
    # Unknown transition value is rejected.
    assert (
        client.get("/api/v1/geofence-alerts", params={"transition": "dwell"}).status_code
        == 422
    )
