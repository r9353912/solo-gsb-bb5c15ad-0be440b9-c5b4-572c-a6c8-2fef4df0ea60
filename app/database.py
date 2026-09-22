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
