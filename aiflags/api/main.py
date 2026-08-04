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


def build_stores() -> tuple[FlagRepository, object]:
    """Build the flag repository and quality store from the environment."""
    dsn = os.environ.get("AIFLAGS_POSTGRES_DSN")
    if not dsn:
        logger.warning(
            "AIFLAGS_POSTGRES_DSN is not set; using in-memory stores. "
            "All flag configuration, audit history and quality evidence will be "
            "lost on restart."
        )
        from aiflags.store.memory import InMemoryFlagRepository
        from aiflags.store.quality import InMemoryQualityStore

        return InMemoryFlagRepository(), InMemoryQualityStore()

    from aiflags.store.postgres import PostgresFlagRepository
    from aiflags.store.quality_postgres import PostgresQualityStore

    repository = PostgresFlagRepository(dsn)
    # The flag repository owns migrations, so the quality store is built after
    # it: quality_samples and rollout_state come from 002.
    return repository, PostgresQualityStore(dsn)


app = create_app(*build_stores())
