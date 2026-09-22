"""Application settings loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    grid_cell_meters: float
    gap_tolerance_seconds: float


def _load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/soundscape.db"),
        grid_cell_meters=float(os.getenv("GRID_CELL_METERS", "50")),
        gap_tolerance_seconds=float(os.getenv("GAP_TOLERANCE_SECONDS", "2")),
    )


settings = _load_settings()
