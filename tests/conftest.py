"""Pytest fixtures: an isolated SQLite database with per-test cleanup."""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["GRID_CELL_METERS"] = "50"
os.environ["GAP_TOLERANCE_SECONDS"] = "5"

from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:  # startup hook runs init_db()
        yield c
    engine.dispose()
    try:
        os.unlink(_tmp_db.name)
    except FileNotFoundError:
        pass


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """Create tables even for tests that do not use the HTTP client."""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """Reset all rows between tests so ids/cluster state is deterministic."""
    from sqlalchemy import text

    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM geofence_alerts"))
        conn.execute(text("DELETE FROM geofence_audit_entries"))
        conn.execute(text("DELETE FROM geofence_devices"))
        conn.execute(text("DELETE FROM geofences"))
        conn.execute(text("DELETE FROM audit_entries"))
        conn.execute(text("DELETE FROM noise_events"))
        conn.execute(text("DELETE FROM clusters"))
