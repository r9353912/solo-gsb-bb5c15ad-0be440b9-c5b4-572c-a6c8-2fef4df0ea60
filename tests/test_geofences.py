"""End-to-end tests for geofence CRUD, bindings and enter/exit alerts."""
from __future__ import annotations

BASE = "/api/v1"


def _square_polygon(lat0=0.0, lon0=0.0, size=10.0, holes=None):
    """A closed axis-aligned square [lat0, lat0+size] x [lon0, lon0+size]."""
    pts = [
        (lat0, lon0),
        (lat0, lon0 + size),
        (lat0 + size, lon0 + size),
        (lat0 + size, lon0),
    ]
    pts.append(pts[0])
    return {
        "outer": [{"latitude": a, "longitude": b} for a, b in pts],
        "holes": holes or [],
    }


def _ring(points):
    closed = points + [points[0]]
    return [{"latitude": a, "longitude": b} for a, b in closed]


def _fence_payload(name="yard", polygon=None, description=None):
    return {
        "name": name,
        "description": description,
        "polygon": polygon or _square_polygon(),
    }


def _event(device_id="dev-1", lat=5.0, lon=5.0,
           start="2026-09-22T08:00:00+08:00",
           end="2026-09-22T08:00:30+08:00"):
    return {
        "device_id": device_id,
        "start_time": start,
        "end_time": end,
        "latitude": lat,
        "longitude": lon,
        "confidence": 0.8,
        "sound_type": "patrol",
    }


def _create_fence(client, **kwargs):
    r = client.post(f"{BASE}/geofences", json=_fence_payload(**kwargs))
    assert r.status_code == 201, r.text
    return r.json()


def _bind(client, fence_id, devices):
    r = client.put(f"{BASE}/geofences/{fence_id}/devices",
                   json={"device_ids": devices})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# CRUD + validation
# ---------------------------------------------------------------------------

def test_create_get_list_geofence(client):
    fence = _create_fence(client)
    assert fence["boundary_rule"] == "outer_included_holes_excluded_v1"
    assert fence["active"] is True
    assert fence["device_ids"] == []
    assert len(fence["polygon"]["outer"]) == 5
    assert fence["crosses_antimeridian"] is False
    detail = client.get(f"{BASE}/geofences/{fence['id']}").json()
    assert "audit_chain" not in fence
    actions = [a["action"] for a in detail["audit_chain"]]
    assert actions == ["fence_created"]
    assert "boundary_rule=" in detail["audit_chain"][0]["detail"]

    listed = client.get(f"{BASE}/geofences").json()
    assert [f["id"] for f in listed] == [fence["id"]]


def test_create_with_holes(client):
    hole = _ring([(3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0)])
    polygon = _square_polygon(holes=[hole])
    fence = _create_fence(client, name="with-hole", polygon=polygon)
    body = client.get(f"{BASE}/geofences/{fence['id']}").json()
    assert len(body["polygon"]["holes"]) == 1
    assert len(body["polygon"]["holes"][0]) == 5


def test_create_invalid_polygon_rejected(client):
    payload = _fence_payload()
    payload["polygon"]["outer"] = payload["polygon"]["outer"][:3]  # unclosed/short
    r = client.post(f"{BASE}/geofences", json=payload)
    assert r.status_code == 422


def test_duplicate_name_conflict(client):
    _create_fence(client, name="dup")
    r = client.post(f"{BASE}/geofences", json=_fence_payload(name="dup"))
    assert r.status_code == 422
    body = _fence_payload(name="dup")
    body["polygon"] = _square_polygon(lat0=20.0)
    r = client.post(f"{BASE}/geofences", json=body)
    assert r.status_code == 422


def test_update_geofence(client):
    fence = _create_fence(client)
    new_polygon = _square_polygon(lat0=1.0, lon0=1.0, size=8.0)
    r = client.patch(
        f"{BASE}/geofences/{fence['id']}",
        json={"description": "updated", "polygon": new_polygon},
    )
    assert r.status_code == 200
    assert r.json()["description"] == "updated"
    detail = client.get(f"{BASE}/geofences/{fence['id']}").json()
    actions = [a["action"] for a in detail["audit_chain"]]
    assert actions == ["fence_created", "fence_updated"]
    assert "boundary_rule=" in detail["audit_chain"][-1]["detail"]


def test_soft_delete(client):
    fence = _create_fence(client)
    assert client.delete(f"{BASE}/geofences/{fence['id']}").status_code == 204
    assert client.get(f"{BASE}/geofences/{fence['id']}").status_code == 404
    assert client.get(f"{BASE}/geofences").json() == []
    # Still viewable with include_deleted, with a deletion audit row.
    detail = client.get(
        f"{BASE}/geofences/{fence['id']}", params={"include_deleted": "true"}
    ).json()
    assert detail["active"] is False
    assert detail["audit_chain"][-1]["action"] == "fence_deleted"


def test_404_geofence(client):
    assert client.get(f"{BASE}/geofences/999").status_code == 404
    assert client.patch(f"{BASE}/geofences/999", json={"description": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# Device bindings
# ---------------------------------------------------------------------------

def test_device_bindings_replace_add_remove(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["a", "b"])
    assert _bind(client, fence["id"], ["b", "c"])["device_ids"] == ["b", "c"]

    r = client.post(f"{BASE}/geofences/{fence['id']}/devices",
                    json={"device_ids": ["d", "b"]})
    assert r.json()["device_ids"] == ["b", "c", "d"]

    r = client.delete(f"{BASE}/geofences/{fence['id']}/devices",
                      params=[("device_id", "c"), ("device_id", "x")])
    assert r.status_code == 200
    assert r.json()["device_ids"] == ["b", "d"]

    detail = client.get(f"{BASE}/geofences/{fence['id']}").json()
    actions = [a["action"] for a in detail["audit_chain"]]
    assert "devices_bound" in actions
    assert "devices_unbound" in actions


def test_deleted_fence_rejects_binding(client):
    fence = _create_fence(client)
    client.delete(f"{BASE}/geofences/{fence['id']}")
    r = client.put(f"{BASE}/geofences/{fence['id']}/devices",
                   json={"device_ids": ["a"]})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Enter/exit state machine
# ---------------------------------------------------------------------------

def test_enter_and_exit_alerts(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])

    # First observation inside: baseline is outside -> enter.
    client.post(f"{BASE}/events", json=_event(lat=5.0, lon=5.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    # Still inside: no new alert.
    client.post(f"{BASE}/events", json=_event(lat=6.0, lon=6.0,
                                              start="2026-09-22T08:01:00+08:00",
                                              end="2026-09-22T08:01:10+08:00"))
    # Moves outside: exit.
    client.post(f"{BASE}/events", json=_event(lat=50.0, lon=50.0,
                                              start="2026-09-22T08:02:00+08:00",
                                              end="2026-09-22T08:02:10+08:00"))
    # Still outside: no alert.
    client.post(f"{BASE}/events", json=_event(lat=51.0, lon=51.0,
                                              start="2026-09-22T08:03:00+08:00",
                                              end="2026-09-22T08:03:10+08:00"))

    page = client.get(f"{BASE}/geofence-alerts",
                      params={"geofence_id": fence["id"]}).json()
    transitions = [(a["device_id"], a["transition"]) for a in page["items"]]
    assert transitions == [("dev-1", "enter"), ("dev-1", "exit")]
    assert page["items"][0]["boundary_rule"] == "outer_included_holes_excluded_v1"
    assert page["next_cursor"] is None
    # Audit chain records both triggered alerts.
    detail = client.get(f"{BASE}/geofences/{fence['id']}").json()
    triggered = [a for a in detail["audit_chain"] if a["action"] == "alert_triggered"]
    assert len(triggered) == 2
    assert [a["detail"].split()[0] for a in triggered] == ["enter", "exit"]


def test_first_event_outside_writes_nothing(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])
    client.post(f"{BASE}/events", json=_event(lat=50.0, lon=50.0))
    page = client.get(f"{BASE}/geofence-alerts").json()
    assert page["items"] == []


def test_unbound_device_generates_no_alerts(client):
    _create_fence(client)  # fence exists but dev-1 is not bound
    client.post(f"{BASE}/events", json=_event(lat=5.0, lon=5.0))
    assert client.get(f"{BASE}/geofence-alerts").json()["items"] == []


def test_two_devices_independent_state(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["a", "b"])
    client.post(f"{BASE}/events", json=_event(device_id="a", lat=5.0, lon=5.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    client.post(f"{BASE}/events", json=_event(device_id="b", lat=50.0, lon=50.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    alerts = client.get(f"{BASE}/geofence-alerts").json()["items"]
    assert [(a["device_id"], a["transition"]) for a in alerts] == [("a", "enter")]


def test_hole_blocks_enter(client):
    hole = _ring([(3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0)])
    fence = _create_fence(client, name="donut", polygon=_square_polygon(holes=[hole]))
    _bind(client, fence["id"], ["dev-1"])
    # Point inside the hole -> stays outside, no alert.
    client.post(f"{BASE}/events", json=_event(lat=5.0, lon=5.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    # Moves into the ring material: enter.
    client.post(f"{BASE}/events", json=_event(lat=8.5, lon=8.5,
                                              start="2026-09-22T08:01:00+08:00",
                                              end="2026-09-22T08:01:10+08:00"))
    alerts = client.get(f"{BASE}/geofence-alerts",
                        params={"geofence_id": fence["id"]}).json()["items"]
    assert [a["transition"] for a in alerts] == ["enter"]


def test_boundary_point_fixed_rule_outer_included(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])
    # Point exactly on the outer edge counts as inside -> enter.
    client.post(f"{BASE}/events", json=_event(lat=0.0, lon=5.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    alerts = client.get(f"{BASE}/geofence-alerts").json()["items"]
    assert [a["transition"] for a in alerts] == ["enter"]
    # Next point just outside the edge -> exit.
    client.post(f"{BASE}/events", json=_event(lat=-0.001, lon=5.0,
                                              start="2026-09-22T08:01:00+08:00",
                                              end="2026-09-22T08:01:10+08:00"))
    transitions = [a["transition"] for a in
                   client.get(f"{BASE}/geofence-alerts").json()["items"]]
    assert transitions == ["enter", "exit"]


def test_antimeridian_fence_alerts(client):
    # Strip 179E..179W (crossing the ±180° meridian), lat 0..10.
    outer = _ring([(0.0, 179.0), (0.0, -179.0), (10.0, -179.0), (10.0, 179.0)])
    fence = _create_fence(
        client, name="dateline", polygon={"outer": outer, "holes": []}
    )
    assert fence["crosses_antimeridian"] is True
    _bind(client, fence["id"], ["ship-1"])

    def obs(lat, lon, minute):
        client.post(f"{BASE}/events", json=_event(
            device_id="ship-1", lat=lat, lon=lon,
            start=f"2026-09-22T08:{minute:02d}:00+08:00",
            end=f"2026-09-22T08:{minute:02d}:10+08:00"))

    obs(5.0, 170.0, 0)    # outside, no alert
    obs(5.0, 179.5, 1)    # enter from the east side
    obs(5.0, -179.5, 2)   # still inside after crossing 180 (no duplicate enter)
    obs(5.0, -170.0, 3)   # exit to the west side
    alerts = client.get(f"{BASE}/geofence-alerts",
                        params={"geofence_id": fence["id"]}).json()["items"]
    assert [(a["transition"], round(a["longitude"], 1)) for a in alerts] == [
        ("enter", 179.5),
        ("exit", -170.0),
    ]


def test_events_evaluated_in_time_order_with_late_arrival(client):
    # Ingest order is not chronological: the state machine must use the
    # chronologically previous event of the device.
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])

    def obs(lat, lon, minute):
        client.post(f"{BASE}/events", json=_event(
            lat=lat, lon=lon,
            start=f"2026-09-22T08:{minute:02d}:00+08:00",
            end=f"2026-09-22T08:{minute:02d}:10+08:00"))

    obs(50.0, 50.0, 3)   # latest so far; baseline outside -> no alert
    obs(5.0, 5.0, 1)     # earlier event inside: prior (t=3 event) is outside -> enter
    obs(5.0, 6.0, 2)     # between: both neighbors inside -> no alert
    alerts = client.get(f"{BASE}/geofence-alerts",
                        params={"geofence_id": fence["id"]}).json()["items"]
    assert len(alerts) == 1
    assert alerts[0]["transition"] == "enter"
    assert alerts[0]["occurred_at"] == "2026-09-22T00:01:00Z"


# ---------------------------------------------------------------------------
# Alert querying: filtering + time keyset pagination
# ---------------------------------------------------------------------------

def _walk(client, params):
    items = []
    cursor = None
    for _ in range(20):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        page = client.get(f"{BASE}/geofence-alerts", params=p).json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    return items


def test_alert_pagination_and_filters(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])
    for minute, inside in [(0, True), (1, False), (2, True), (3, False), (4, True)]:
        lat, lon = (5.0, 5.0) if inside else (50.0, 50.0)
        client.post(f"{BASE}/events", json=_event(
            lat=lat, lon=lon,
            start=f"2026-09-22T08:{minute:02d}:00+08:00",
            end=f"2026-09-22T08:{minute:02d}:10+08:00"))

    all_alerts = _walk(client, {"limit": 2})
    assert len(all_alerts) == 5
    times = [a["occurred_at"] for a in all_alerts]
    assert times == sorted(times)

    enters = client.get(f"{BASE}/geofence-alerts",
                        params={"transition": "enter"}).json()["items"]
    assert len(enters) == 3

    windowed = client.get(
        f"{BASE}/geofence-alerts",
        params={
            "occurred_after": "2026-09-22T00:01:30Z",
            "occurred_before": "2026-09-22T00:04:00Z",
        },
    ).json()["items"]
    assert [a["occurred_at"] for a in windowed] == [
        "2026-09-22T00:02:00Z",
        "2026-09-22T00:03:00Z",
    ]

    bad = client.get(f"{BASE}/geofence-alerts", params={"cursor": "not-a-cursor"})
    assert bad.status_code == 422
    assert client.get(f"{BASE}/geofence-alerts",
                      params={"geofence_id": 999}).status_code == 404


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def test_replay_recomputes_alerts_from_history(client):
    # History exists before the fence is bound to the device: live ingest
    # produced no alerts; replay must reconstruct them.
    for minute, inside in [(0, True), (1, True), (2, False), (3, True)]:
        lat, lon = (5.0, 5.0) if inside else (50.0, 50.0)
        client.post(f"{BASE}/events", json=_event(
            lat=lat, lon=lon,
            start=f"2026-09-22T08:{minute:02d}:00+08:00",
            end=f"2026-09-22T08:{minute:02d}:10+08:00"))
    assert client.get(f"{BASE}/geofence-alerts").json()["items"] == []

    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])
    r = client.post(f"{BASE}/geofences/{fence['id']}/replay")
    assert r.status_code == 200
    body = r.json()
    assert body["alerts_created"] == 3
    assert body["deleted_alert_ids"] == []

    alerts = client.get(f"{BASE}/geofence-alerts",
                        params={"geofence_id": fence["id"]}).json()["items"]
    assert [a["transition"] for a in alerts] == ["enter", "exit", "enter"]
    assert all(a["replayed"] is True for a in alerts)

    detail = client.get(f"{BASE}/geofences/{fence['id']}").json()
    actions = [a["action"] for a in detail["audit_chain"]]
    assert actions.count("alert_triggered") == 3
    assert actions[-1] == "alerts_replayed"

    # Replaying again replaces, not appends: same 3 alerts, old ids listed.
    old_ids = [a["id"] for a in alerts]
    body2 = client.post(f"{BASE}/geofences/{fence['id']}/replay").json()
    assert body2["alerts_created"] == 3
    assert body2["deleted_alert_ids"] == old_ids
    alerts2 = client.get(f"{BASE}/geofence-alerts",
                         params={"geofence_id": fence["id"]}).json()["items"]
    assert [a["transition"] for a in alerts2] == ["enter", "exit", "enter"]
    # The audit chain holds one triggered row per *current* alert (3), not 6,
    # and one summary row per replay invocation (2): replacement, not append.
    detail2 = client.get(f"{BASE}/geofences/{fence['id']}").json()
    actions2 = [a["action"] for a in detail2["audit_chain"]]
    assert actions2.count("alert_triggered") == 3
    assert actions2.count("alerts_replayed") == 2


def test_replay_after_polygon_edit(client):
    fence = _create_fence(client)
    _bind(client, fence["id"], ["dev-1"])
    client.post(f"{BASE}/events", json=_event(lat=5.0, lon=5.0,
                                              start="2026-09-22T08:00:00+08:00",
                                              end="2026-09-22T08:00:10+08:00"))
    assert len(client.get(f"{BASE}/geofence-alerts").json()["items"]) == 1
    # Shrink the polygon away from the observation point, then replay.
    small = _square_polygon(lat0=40.0, lon0=40.0, size=2.0)
    client.patch(f"{BASE}/geofences/{fence['id']}", json={"polygon": small})
    client.post(f"{BASE}/geofences/{fence['id']}/replay")
    assert client.get(f"{BASE}/geofence-alerts",
                      params={"geofence_id": fence["id"]}).json()["items"] == []


def test_existing_event_endpoints_unchanged(client):
    # The events API response shape is untouched by geofence evaluation.
    r = client.post(f"{BASE}/events", json=_event())
    assert r.status_code == 201
    assert set(r.json()) == {"created", "cluster_id", "event_id", "merged_cluster_ids"}
