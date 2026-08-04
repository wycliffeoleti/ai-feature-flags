"""PostgreSQL flag repository.

Durable side of the control plane. Satisfies the same contract as
:class:`~aiflags.store.memory.InMemoryFlagRepository` and is verified by the same
test body, so the two cannot drift.

Each mutation writes the flag, bumps the single snapshot counter, and appends an
audit row inside one transaction. Doing them separately would allow a published
configuration with no record of who caused it — precisely the case an audit trail
exists for.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from aiflags.core.models import FlagDefinition, FlagSnapshot, FlagStatus
from aiflags.store.base import (
    AuditEvent,
    FlagAlreadyExists,
    FlagNotFound,
    flag_from_dict,
    flag_to_dict,
    require_attribution,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class PostgresFlagRepository:
    """Durable implementation of :class:`~aiflags.store.base.FlagRepository`."""

    def __init__(self, dsn: str, migrate: bool = True) -> None:
        self._dsn = dsn
        # psycopg connections are not thread-safe; the API and the controller
        # both touch this store, so serialise at the repository boundary.
        self._lock = threading.RLock()
        self._connection = psycopg.connect(dsn, row_factory=dict_row)
        if migrate:
            self.migrate()

    def close(self) -> None:
        self._connection.close()

    def migrate(self) -> None:
        """Apply every migration file in name order. Idempotent by construction."""
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    cursor.execute(path.read_text(encoding="utf-8"))

    def reset_for_tests(self) -> None:
        """Truncate all state. Only used by the contract test."""
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute("TRUNCATE flags, audit_events RESTART IDENTITY")
                cursor.execute("UPDATE snapshot_version SET version = 0")

    # -- reads -------------------------------------------------------------- #

    def get_flag(self, key: str) -> FlagDefinition | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT key, status, rollout_percentage, definition "
                "FROM flags WHERE key = %s",
                (key,),
            )
            row = cursor.fetchone()
        return _row_to_flag(row) if row else None

    def list_flags(self) -> list[FlagDefinition]:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT key, status, rollout_percentage, definition "
                "FROM flags ORDER BY key"
            )
            rows = cursor.fetchall()
        return [_row_to_flag(row) for row in rows]

    def snapshot(self) -> FlagSnapshot:
        # One transaction so the version and the flags it describes are read
        # consistently; otherwise a concurrent mutation could produce a snapshot
        # whose version does not match its contents.
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT version FROM snapshot_version")
                version = cursor.fetchone()["version"]
                cursor.execute(
                    "SELECT key, status, rollout_percentage, definition FROM flags"
                )
                rows = cursor.fetchall()
        return FlagSnapshot(
            version=version,
            published_at=datetime.now(UTC),
            flags={row["key"]: _row_to_flag(row) for row in rows},
        )

    def audit_events(self, flag_key: str | None = None) -> list[AuditEvent]:
        query = (
            "SELECT flag_key, action, actor, reason, at, snapshot_version, detail "
            "FROM audit_events"
        )
        params: tuple[Any, ...] = ()
        if flag_key is not None:
            query += " WHERE flag_key = %s"
            params = (flag_key,)
        query += " ORDER BY id"

        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [
            AuditEvent(
                flag_key=row["flag_key"],
                action=row["action"],
                actor=row["actor"],
                reason=row["reason"],
                at=row["at"],
                snapshot_version=row["snapshot_version"],
                detail=row["detail"],
            )
            for row in rows
        ]

    # -- writes ------------------------------------------------------------- #

    def create_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int:
        require_attribution(actor, reason)
        document, status, percentage = _split(flag)
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM flags WHERE key = %s", (flag.key,))
                if cursor.fetchone():
                    raise FlagAlreadyExists(flag.key)
                cursor.execute(
                    "INSERT INTO flags (key, status, rollout_percentage, definition) "
                    "VALUES (%s, %s, %s, %s)",
                    (flag.key, status, percentage, Jsonb(document)),
                )
                return self._record(cursor, flag.key, "create_flag", actor, reason, {})

    def replace_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int:
        require_attribution(actor, reason)
        document, status, percentage = _split(flag)
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE flags SET status = %s, rollout_percentage = %s, "
                    "definition = %s, updated_at = now() WHERE key = %s",
                    (status, percentage, Jsonb(document), flag.key),
                )
                if cursor.rowcount == 0:
                    raise FlagNotFound(flag.key)
                return self._record(cursor, flag.key, "replace_flag", actor, reason, {})

    def set_rollout_percentage(
        self, key: str, percentage: float, *, actor: str, reason: str
    ) -> int:
        require_attribution(actor, reason)
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE flags SET rollout_percentage = %s, updated_at = now() "
                    "WHERE key = %s",
                    (percentage, key),
                )
                if cursor.rowcount == 0:
                    raise FlagNotFound(key)
                return self._record(
                    cursor,
                    key,
                    "set_rollout_percentage",
                    actor,
                    reason,
                    {"percentage": percentage},
                )

    def set_status(
        self, key: str, status: FlagStatus, *, actor: str, reason: str
    ) -> int:
        require_attribution(actor, reason)
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE flags SET status = %s, updated_at = now() WHERE key = %s",
                    (status.value, key),
                )
                if cursor.rowcount == 0:
                    raise FlagNotFound(key)
                return self._record(
                    cursor, key, "set_status", actor, reason, {"status": status.value}
                )

    def rollback(self, key: str, *, actor: str, reason: str) -> int:
        """Set status and percentage together, as one audited event.

        Two separate calls would leave a window in which the flag reads as
        ROLLED_BACK while still serving a ramped percentage, and would split one
        operational decision across two audit entries.
        """
        require_attribution(actor, reason)
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE flags SET status = %s, rollout_percentage = 0, "
                    "updated_at = now() WHERE key = %s "
                    "RETURNING (SELECT rollout_percentage FROM flags WHERE key = %s)",
                    (FlagStatus.ROLLED_BACK.value, key, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise FlagNotFound(key)
                return self._record(
                    cursor,
                    key,
                    "rollback",
                    actor,
                    reason,
                    {
                        "percentage": 0.0,
                        "previous_percentage": row["rollout_percentage"],
                    },
                )

    # -- internals ---------------------------------------------------------- #

    def _record(
        self,
        cursor: psycopg.Cursor,
        flag_key: str,
        action: str,
        actor: str,
        reason: str,
        detail: dict[str, Any],
    ) -> int:
        """Bump the snapshot version and append the audit row, same transaction."""
        cursor.execute(
            "UPDATE snapshot_version SET version = version + 1 RETURNING version"
        )
        version = cursor.fetchone()["version"]
        cursor.execute(
            "INSERT INTO audit_events "
            "(flag_key, action, actor, reason, snapshot_version, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (flag_key, action, actor, reason, version, Jsonb(detail)),
        )
        return version


def _split(flag: FlagDefinition) -> tuple[dict[str, Any], str, float]:
    """Separate the queryable columns from the JSONB document.

    Status and percentage are removed from the document so there is exactly one
    copy of each fact. Two copies drift, and the one the data plane reads is the
    one that would go stale.
    """
    document = flag_to_dict(flag)
    status = document.pop("status")
    percentage = document.pop("rollout_percentage")
    return document, status, percentage


def _row_to_flag(row: dict[str, Any]) -> FlagDefinition:
    document = dict(row["definition"])
    document["status"] = row["status"]
    document["rollout_percentage"] = row["rollout_percentage"]
    return flag_from_dict(document)
