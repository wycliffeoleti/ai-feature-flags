"""In-memory flag repository.

Backs the API tests and the offline demo. It goes through the same
serialisation helpers as the PostgreSQL store rather than holding live objects,
so a field that would not survive a database round trip fails here too — an
in-memory store that quietly accepts more than the real one is worse than no
test at all.
"""

from __future__ import annotations

import copy
import threading
from datetime import UTC, datetime

from aiflags.core.models import FlagDefinition, FlagSnapshot, FlagStatus
from aiflags.store.base import (
    AuditEvent,
    FlagAlreadyExists,
    FlagNotFound,
    flag_from_dict,
    flag_to_dict,
    require_attribution,
)


class InMemoryFlagRepository:
    """Thread-safe, process-local implementation of :class:`FlagRepository`."""

    def __init__(self) -> None:
        self._flags: dict[str, dict] = {}
        self._audit: list[AuditEvent] = []
        self._version = 0
        self._lock = threading.RLock()

    # -- reads -------------------------------------------------------------- #

    def get_flag(self, key: str) -> FlagDefinition | None:
        with self._lock:
            payload = self._flags.get(key)
            return flag_from_dict(copy.deepcopy(payload)) if payload else None

    def list_flags(self) -> list[FlagDefinition]:
        with self._lock:
            return [
                flag_from_dict(copy.deepcopy(payload))
                for payload in self._flags.values()
            ]

    def snapshot(self) -> FlagSnapshot:
        with self._lock:
            return FlagSnapshot(
                version=self._version,
                published_at=datetime.now(UTC),
                flags={
                    key: flag_from_dict(copy.deepcopy(payload))
                    for key, payload in self._flags.items()
                },
            )

    def audit_events(self, flag_key: str | None = None) -> list[AuditEvent]:
        with self._lock:
            # A copy, so a caller cannot mutate the trail it was handed.
            return [
                event
                for event in self._audit
                if flag_key is None or event.flag_key == flag_key
            ]

    # -- writes ------------------------------------------------------------- #

    def create_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int:
        require_attribution(actor, reason)
        with self._lock:
            if flag.key in self._flags:
                raise FlagAlreadyExists(flag.key)
            self._flags[flag.key] = flag_to_dict(flag)
            return self._record(flag.key, "create_flag", actor, reason, {})

    def replace_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int:
        require_attribution(actor, reason)
        with self._lock:
            self._require(flag.key)
            self._flags[flag.key] = flag_to_dict(flag)
            return self._record(flag.key, "replace_flag", actor, reason, {})

    def set_rollout_percentage(
        self, key: str, percentage: float, *, actor: str, reason: str
    ) -> int:
        require_attribution(actor, reason)
        with self._lock:
            payload = self._require(key)
            payload["rollout_percentage"] = percentage
            return self._record(
                key, "set_rollout_percentage", actor, reason, {"percentage": percentage}
            )

    def set_status(
        self, key: str, status: FlagStatus, *, actor: str, reason: str
    ) -> int:
        require_attribution(actor, reason)
        with self._lock:
            payload = self._require(key)
            payload["status"] = status.value
            return self._record(
                key, "set_status", actor, reason, {"status": status.value}
            )

    def rollback(self, key: str, *, actor: str, reason: str) -> int:
        """Set status and percentage together, as one audited event.

        Doing this as two calls would leave a window in which the flag is
        ROLLED_BACK but still ramped, and would split one operational decision
        across two audit entries.
        """
        require_attribution(actor, reason)
        with self._lock:
            payload = self._require(key)
            previous = payload["rollout_percentage"]
            payload["status"] = FlagStatus.ROLLED_BACK.value
            payload["rollout_percentage"] = 0.0
            return self._record(
                key,
                "rollback",
                actor,
                reason,
                {"percentage": 0.0, "previous_percentage": previous},
            )

    # -- internals ---------------------------------------------------------- #

    def _require(self, key: str) -> dict:
        payload = self._flags.get(key)
        if payload is None:
            raise FlagNotFound(key)
        return payload

    def _record(
        self, flag_key: str, action: str, actor: str, reason: str, detail: dict
    ) -> int:
        self._version += 1
        self._audit.append(
            AuditEvent(
                flag_key=flag_key,
                action=action,
                actor=actor,
                reason=reason,
                at=datetime.now(UTC),
                snapshot_version=self._version,
                detail=detail,
            )
        )
        return self._version
