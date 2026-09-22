"""End-to-end tests for the clustering API."""
from __future__ import annotations


def _event(
    device_id="sensor-A",
    start="2026-09-22T08:00:00+08:00",
    end="2026-09-22T08:00:30+08:00",
    lat=31.2304,
    lon=121.4737,
    confidence=0.8,
    sound_type="traffic",
):
    return {
        "device_id": device_id,
        "start_time": start,
        "end_time": end,
        "latitude": lat,
        "longitude": lon,
        "confidence": confidence,
        "sound_type": sound_type,
    }


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_naive_timezone_rejected(client):
    payload = _event(start="2026-09-22T08:00:00", end="2026-09-22T08:00:30")
    r = client.post("/api/v1/events", json=payload)
    assert r.status_code == 422


def test_invalid_interval_rejected(client):
    payload = _event(
        start="2026-09-22T08:00:30+08:00",
        end="2026-09-22T08:00:00+08:00",
    )
    r = client.post("/api/v1/events", json=payload)
    assert r.status_code == 422


def test_first_event_creates_cluster(client):
    r = client.post("/api/v1/events", json=_event())
    assert r.status_code == 201
    body = r.json()
    assert body["cluster_id"] == 1
    assert body["event_id"] == 1
    assert body["merged_cluster_ids"] == []

    detail = client.get("/api/v1/clusters/1").json()
    assert detail["event_count"] == 1
    assert detail["representative_event_id"] == 1
    assert detail["device_ids"] == ["sensor-A"]
    actions = [a["action"] for a in detail["audit_chain"]]
    assert "cluster_created" in actions
    assert "event_added" in actions


def test_same_location_overlapping_time_merges(client):
    client.post("/api/v1/events", json=_event(device_id="A", confidence=0.6))
    r = client.post(
        "/api/v1/events", json=_event(device_id="B", confidence=0.95)
    )
    assert r.status_code == 201
    assert r.json()["cluster_id"] == 1

    detail = client.get("/api/v1/clusters/1").json()
    assert detail["event_count"] == 2
    assert sorted(detail["device_ids"]) == ["A", "B"]
    # Highest confidence record wins as representative.
    assert detail["representative_event_id"] == 2
    assert detail["confidence"] == 0.95

    conflicts = [a for a in detail["audit_chain"] if a["action"] == "event_conflict"]
    assert len(conflicts) == 1
    entry = conflicts[0]
    assert entry["event_id"] == 1
    assert entry["device_id"] == "A"
    assert "event_id=2" in entry["detail"]


def test_conflict_audit_chain_order_and_payload(client):
    # Three devices observe the same interval; only highest confidence survives.
    client.post("/api/v1/events", json=_event(device_id="A", confidence=0.5))
    client.post("/api/v1/events", json=_event(device_id="B", confidence=0.7))
    client.post("/api/v1/events", json=_event(device_id="C", confidence=0.9))

    detail = client.get("/api/v1/clusters/1").json()
    assert detail["representative_event_id"] == 3
    assert detail["event_count"] == 3
    seqs = [a["seq"] for a in detail["audit_chain"]]
    assert seqs == sorted(seqs)
    losers = {
        a["event_id"]
        for a in detail["audit_chain"]
        if a["action"] == "event_conflict"
    }
    assert losers == {1, 2}


def test_gap_within_tolerance_merges(client):
    # Gap of 3 seconds <= tolerance (5s): same cluster, no strict overlap.
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="A",
            start="2026-09-22T08:00:00+08:00",
            end="2026-09-22T08:00:30+08:00",
        ),
    )
    r = client.post(
        "/api/v1/events",
        json=_event(
            device_id="B",
            start="2026-09-22T08:00:33+08:00",
            end="2026-09-22T08:01:00+08:00",
        ),
    )
    assert r.json()["cluster_id"] == 1
    detail = client.get("/api/v1/clusters/1").json()
    assert not any(a["action"] == "event_conflict" for a in detail["audit_chain"])


def test_gap_beyond_tolerance_separates(client):
    # Gap of 30 seconds > tolerance: separate clusters.
    client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:00:00+08:00", end="2026-09-22T08:00:30+08:00"
        ),
    )
    r = client.post(
        "/api/v1/events",
        json=_event(
            start="2026-09-22T08:01:00+08:00", end="2026-09-22T08:01:30+08:00"
        ),
    )
    assert r.json()["cluster_id"] == 2
    assert len(client.get("/api/v1/clusters").json()) == 2


def test_far_locations_do_not_merge(client):
    # Two points about 1 km apart, same time: different 50 m grid cells.
    client.post("/api/v1/events", json=_event(lat=31.2304, lon=121.4737))
    r = client.post("/api/v1/events", json=_event(lat=31.2394, lon=121.4737))
    assert r.json()["cluster_id"] == 2


def test_transitive_chain_merges_three_clusters(client):
    # A and B sit exactly two 50 m grid cells apart (two separate clusters);
    # C sits in the cell between them and overlaps both in time, bridging the
    # clusters transitively. Coordinates are computed from the real grid so the
    # test does not depend on cell-boundary phase.
    import math

    earth_radius_m = 6_371_000.0
    m_per_deg_lat = math.pi * earth_radius_m / 180.0

    def lat_for_cell(cell_y: int, inset_m: float = 2.0) -> float:
        return (cell_y * 50.0 + inset_m) / m_per_deg_lat

    lat_a = lat_for_cell(10_000)
    lat_c = lat_for_cell(10_001)
    lat_b = lat_for_cell(10_002)

    client.post(
        "/api/v1/events",
        json=_event(device_id="A", lat=lat_a, lon=121.47000, confidence=0.4),
    )
    client.post(
        "/api/v1/events",
        json=_event(device_id="B", lat=lat_b, lon=121.47000, confidence=0.9),
    )
    assert len(client.get("/api/v1/clusters").json()) == 2

    r = client.post(
        "/api/v1/events",
        json=_event(device_id="C", lat=lat_c, lon=121.47000, confidence=0.6),
    )
    # The third event bridges clusters 1 and 2; one gets absorbed.
    assert r.status_code == 201
    surviving_id = r.json()["cluster_id"]
    assert r.json()["merged_cluster_ids"] == [1 if surviving_id == 2 else 2]

    active = client.get("/api/v1/clusters").json()
    assert len(active) == 1
    detail = client.get(f"/api/v1/clusters/{surviving_id}").json()
    assert detail["event_count"] == 3
    # The 0.9 confidence record remains the representative.
    assert detail["confidence"] == 0.9
    assert sorted(detail["device_ids"]) == ["A", "B", "C"]
    # Lineage walks back through at least one absorbed cluster.
    assert len(detail["lineage"]) >= 2
    absorbed_id = next(cid for cid in detail["lineage"] if cid != surviving_id)
    absorbed = client.get(f"/api/v1/clusters/{absorbed_id}").json()
    assert absorbed["active"] is False
    assert absorbed["merged_into_id"] == surviving_id
    assert any(a["action"] == "cluster_merged" for a in absorbed["audit_chain"])


def test_chain_extends_through_distant_members(client):
    # After a transitive merge the cluster spans cells 10000..10002 anchored at
    # the minimum cell. A fourth event in cell 10003 is adjacent only to the
    # far member (cell 10002) and must still join the existing cluster.
    import math

    m_per_deg_lat = math.pi * 6_371_000.0 / 180.0

    def lat_for_cell(cell_y: int) -> float:
        return (cell_y * 50.0 + 2.0) / m_per_deg_lat

    body = [
        _event(device_id="A", lat=lat_for_cell(10_000), lon=121.47),
        _event(device_id="B", lat=lat_for_cell(10_002), lon=121.47),
        _event(device_id="C", lat=lat_for_cell(10_001), lon=121.47),
        _event(device_id="D", lat=lat_for_cell(10_003), lon=121.47),
    ]
    r = client.post("/api/v1/events/batch", json=body)
    assert r.status_code == 201
    # Event B initially creates cluster 2, later absorbed when C bridges.
    surviving_id = r.json()[-1]["cluster_id"]

    # Final state: all four events attached to one surviving cluster.
    events = client.get("/api/v1/events").json()
    assert {e["cluster_id"] for e in events} == {surviving_id}
    detail = client.get(f"/api/v1/clusters/{surviving_id}").json()
    assert detail["event_count"] == 4
    assert len(client.get("/api/v1/clusters").json()) == 1


def test_batch_ingest_and_listing_filters(client):
    payload = [
        _event(device_id="A", start="2026-09-22T08:00:00+08:00", end="2026-09-22T08:00:20+08:00"),
        _event(device_id="A", start="2026-09-22T08:00:10+08:00", end="2026-09-22T08:00:40+08:00"),
        _event(
            device_id="B",
            lat=31.30,
            lon=121.60,
            start="2026-09-22T09:00:00+08:00",
            end="2026-09-22T09:00:20+08:00",
        ),
    ]
    r = client.post("/api/v1/events/batch", json=payload)
    assert r.status_code == 201
    assert len(r.json()) == 3

    events_a = client.get("/api/v1/events", params={"device_id": "A"}).json()
    assert len(events_a) == 2

    clusters = client.get(
        "/api/v1/clusters",
        params={"min_lat": 31.29, "max_lat": 31.31, "min_lon": 121.59, "max_lon": 121.61},
    ).json()
    assert len(clusters) == 1


def test_empty_batch_rejected(client):
    r = client.post("/api/v1/events/batch", json=[])
    assert r.status_code == 422


def test_timezone_normalization_and_utc_output(client):
    # Same instant expressed in +08:00 and +00:00 must overlap and merge.
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="east",
            start="2026-09-22T08:00:00+08:00",
            end="2026-09-22T08:00:30+08:00",
            confidence=0.8,
        ),
    )
    client.post(
        "/api/v1/events",
        json=_event(
            device_id="utc",
            start="2026-09-22T00:00:05+00:00",
            end="2026-09-22T00:00:20+00:00",
            confidence=0.9,
        ),
    )
    detail = client.get("/api/v1/clusters/1").json()
    assert detail["event_count"] == 2
    # UTC normalization (FastAPI renders UTC with a trailing Z).
    assert detail["started_at"] == "2026-09-22T00:00:00Z"

    east_event = client.get("/api/v1/events/1").json()
    assert east_event["start_time"] == "2026-09-22T00:00:00Z"
    assert east_event["start_time_raw"] == "2026-09-22T08:00:00+08:00"
    assert east_event["tz_offset_minutes"] == 480


def test_rebuild_recomputes_clusters(client):
    # Two far-apart clusters first.
    client.post(
        "/api/v1/events",
        json=_event(device_id="A", lat=31.2304, lon=121.4737, confidence=0.5),
    )
    client.post(
        "/api/v1/events",
        json=_event(device_id="B", lat=31.3000, lon=121.6000, confidence=0.8),
    )
    assert len(client.get("/api/v1/clusters").json()) == 2

    r = client.post("/api/v1/clusters/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["cluster_count"] == 2
    assert body["event_count"] == 2

    # Another event at the first location: rebuild keeps both clusters.
    client.post(
        "/api/v1/events",
        json=_event(device_id="A", lat=31.2305, lon=121.4738, confidence=0.5,
                    start="2026-09-22T08:00:00+08:00", end="2026-09-22T08:00:20+08:00"),
    )
    r = client.post("/api/v1/clusters/rebuild")
    assert r.json()["event_count"] == 3
    assert len(client.get("/api/v1/clusters").json()) == 2


def test_404s(client):
    assert client.get("/api/v1/events/999").status_code == 404
    assert client.get("/api/v1/clusters/999").status_code == 404
