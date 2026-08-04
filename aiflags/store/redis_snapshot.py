"""Redis-published flag snapshot — the data plane's read path.

The control plane writes PostgreSQL; this publishes the resulting snapshot to
Redis, and every SDK instance polls Redis rather than the API. That keeps the
read path off the database entirely and means an API outage does not stop
applications from evaluating flags — they keep reading the last published
snapshot.

Redis is the right home for this precisely because losing it is survivable. If
the key is evicted or the server restarts, SDKs keep serving their cached
snapshot and the publisher rewrites it on the next tick. Contrast the quality
evidence, which lives in PostgreSQL because a rollback decision must be
justifiable from data that cannot vanish.

Publishing is version-guarded: an older snapshot never overwrites a newer one,
so two publishers racing cannot move the data plane backwards.
"""

from __future__ import annotations

import json
from datetime import datetime

import redis

from aiflags.core.models import FlagSnapshot
from aiflags.store.base import flag_from_dict, flag_to_dict

DEFAULT_SNAPSHOT_KEY = "aiflags:snapshot"


class RedisSnapshotStore:
    """Publishes and reads the current flag snapshot.

    Implements the SDK's ``SnapshotSource`` protocol via :meth:`fetch`.
    """

    def __init__(
        self,
        client: redis.Redis,
        key: str = DEFAULT_SNAPSHOT_KEY,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._key = key
        self._ttl = ttl_seconds

    @classmethod
    def from_url(cls, url: str, **kwargs) -> RedisSnapshotStore:
        return cls(redis.Redis.from_url(url, decode_responses=True), **kwargs)

    def publish(self, snapshot: FlagSnapshot) -> bool:
        """Publish a snapshot. Returns whether it was applied.

        A snapshot older than or equal to the published one is rejected rather
        than written: two publishers racing must not be able to move the data
        plane back onto a percentage an operator already changed.
        """
        current = self._read_raw()
        if current is not None and current["version"] >= snapshot.version:
            return False
        self._client.set(self._key, json.dumps(encode(snapshot)), ex=self._ttl)
        return True

    def fetch(self) -> FlagSnapshot | None:
        """Read the published snapshot, or ``None`` if nothing is published."""
        payload = self._read_raw()
        return decode(payload) if payload is not None else None

    def clear(self) -> None:
        self._client.delete(self._key)

    def _read_raw(self) -> dict | None:
        raw = self._client.get(self._key)
        if raw is None:
            return None
        return json.loads(raw)


def encode(snapshot: FlagSnapshot) -> dict:
    return {
        "version": snapshot.version,
        "published_at": snapshot.published_at.isoformat(),
        "flags": {key: flag_to_dict(flag) for key, flag in snapshot.flags.items()},
    }


def decode(payload: dict) -> FlagSnapshot:
    return FlagSnapshot(
        version=payload["version"],
        published_at=datetime.fromisoformat(payload["published_at"]),
        flags={
            key: flag_from_dict(flag) for key, flag in payload.get("flags", {}).items()
        },
    )
