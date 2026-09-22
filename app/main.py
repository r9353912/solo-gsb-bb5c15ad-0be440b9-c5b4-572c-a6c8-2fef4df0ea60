"""FastAPI application entrypoint.

The database is initialized (tables created) on startup, so the container is
ready to serve immediately once uvicorn is listening.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import init_db
from .routers import router as api_router

logger = logging.getLogger("soundscape")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    logger.info("initializing database schema")
    init_db()
    logger.info("startup complete")
    yield


app = FastAPI(
    title="Soundscape Inspection Event Merge API",
    description=(
        "Ingest timezone-aware noise events from inspection devices and merge "
        "them into clusters by location grid adjacency and time proximity. "
        "Overlapping duplicate observations are resolved by confidence with a "
        "full per-cluster audit chain."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(api_router)
