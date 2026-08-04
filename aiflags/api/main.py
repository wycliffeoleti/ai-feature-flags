"""ASGI entrypoint for the management API.

Run with::

    AIFLAGS_POSTGRES_DSN=postgresql://... uv run uvicorn aiflags.api.main:app

With no DSN configured the service starts against the in-memory repository. That
is deliberate for local exploration and the offline demo, and it logs a warning
loudly enough that nobody mistakes it for a durable deployment.
"""

from __future__ import annotations

import logging
import os

from aiflags.api.app import create_app
from aiflags.store.base import FlagRepository

logger = logging.getLogger(__name__)


def build_repository() -> FlagRepository:
    dsn = os.environ.get("AIFLAGS_POSTGRES_DSN")
    if not dsn:
        logger.warning(
            "AIFLAGS_POSTGRES_DSN is not set; using the in-memory repository. "
            "All flag configuration and audit history will be lost on restart."
        )
        from aiflags.store.memory import InMemoryFlagRepository

        return InMemoryFlagRepository()

    from aiflags.store.postgres import PostgresFlagRepository

    return PostgresFlagRepository(dsn)


app = create_app(build_repository())
