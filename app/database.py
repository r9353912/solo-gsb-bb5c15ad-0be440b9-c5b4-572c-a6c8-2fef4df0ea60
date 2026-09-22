"""SQLAlchemy engine, session factory and ORM models.

The schema is intentionally normalized without foreign-key cascades between
clusters and events: a cluster can be absorbed into another one (``merged_into``)
and events are re-attached, while audit rows keep pointing at the original
cluster ids to preserve the full audit chain.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Cluster(Base):
    """A merged group of temporally/spatially adjacent noise events."""

    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cell_x: Mapped[int] = mapped_column(Integer, nullable=False)
    cell_y: Mapped[int] = mapped_column(Integer, nullable=False)
    device_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The representative (highest-confidence overlapping) member event.
    representative_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("noise_events.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[object] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[object] = mapped_column(DateTime, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("ix_clusters_cells", "cell_x", "cell_y"),
        Index("ix_clusters_times", "started_at", "ended_at"),
        Index("ix_clusters_active", "active"),
        Index("ix_clusters_merged_into", "merged_into_id"),
    )


class NoiseEvent(Base):
    """A raw noise event reported by a single device."""

    __tablename__ = "noise_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # All datetimes are stored normalized to UTC.
    start_time: Mapped[object] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[object] = mapped_column(DateTime, nullable=False)
    start_time_raw: Mapped[str] = mapped_column(String(64), nullable=False)
    end_time_raw: Mapped[str] = mapped_column(String(64), nullable=False)
    tz_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    cell_x: Mapped[int] = mapped_column(Integer, nullable=False)
    cell_y: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sound_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    observed_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("ix_events_cells", "cell_x", "cell_y"),
        Index("ix_events_times", "start_time", "end_time"),
        Index("ix_events_cluster", "cluster_id"),
    )


class AuditEntry(Base):
    """Append-only audit chain entry for a cluster.

    Actions:
      * ``event_added``      – an event joined the cluster
      * ``event_conflict``   – overlapping event lost to a higher-confidence one
      * ``cluster_created``  – cluster born with its first event
      * ``cluster_merged``   – cluster was absorbed into another cluster
      * ``cluster_rebuilt``  – cluster produced by a rebuild-from-events run
    """

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("ix_audit_cluster_seq", "cluster_id", "seq", unique=True),
    )


class Geofence(Base):
    """An inspection geofence: one outer lat/lon ring minus optional holes.

    ``rings_json`` stores ``{"outer": [[lat, lon], ...],
    "holes": [[[lat, lon], ...], ...]}``; ``boundary_rule`` pins the exact
    containment semantics used for every historical decision.
    """

    __tablename__ = "geofences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rings_json: Mapped[str] = mapped_column(Text, nullable=False)
    boundary_rule: Mapped[str] = mapped_column(String(64), nullable=False)
    crosses_antimeridian: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class GeofenceDevice(Base):
    """Binding of a reporting device to a geofence (many-to-many)."""

    __tablename__ = "geofence_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    geofence_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("geofences.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("geofence_id", "device_id", name="uq_geofence_device"),
        Index("ix_geofence_devices_device", "device_id"),
    )


class GeofenceAlert(Base):
    """An entry/exit alert, written only when a device's in/out state changes."""

    __tablename__ = "geofence_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    geofence_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("geofences.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    transition: Mapped[str] = mapped_column(String(8), nullable=False)  # enter | exit
    # UTC time of the triggering observation (noise_events.start_time).
    occurred_at: Mapped[object] = mapped_column(DateTime, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("noise_events.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshot keeps alerts readable even if a fence is renamed.
    geofence_name: Mapped[str] = mapped_column(String(128), nullable=False)
    boundary_rule: Mapped[str] = mapped_column(String(64), nullable=False)
    replayed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index(
            "ix_geofence_alerts_time_id",
            "occurred_at",
            "id",
        ),
        Index("ix_geofence_alerts_fence_time", "geofence_id", "occurred_at"),
        Index("ix_geofence_alerts_device_time", "device_id", "occurred_at"),
    )


class GeofenceAuditEntry(Base):
    """Append-only audit chain for a geofence (and its alerts).

    Actions:
      * ``fence_created``      – fence saved (boundary rule recorded in detail)
      * ``fence_updated``      – polygon/metadata changed
      * ``fence_deleted``      – fence soft-deleted
      * ``devices_bound``      – devices attached
      * ``devices_unbound``    – devices detached
      * ``alert_triggered``    – an enter/exit alert was written
      * ``alerts_replayed``    – alerts were recomputed from the event history
    """

    __tablename__ = "geofence_audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    geofence_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("geofences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alert_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("ix_geofence_audit_fence_seq", "geofence_id", "seq", unique=True),
    )


def _sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    import sqlite3

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
if settings.database_url.startswith("sqlite"):
    event.listen(engine, "connect", _sqlite_pragmas)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables on startup (idempotent)."""
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
