"""Outcome queue between the application and the quality evaluator.

The SDK hands outcomes here and returns immediately; the evaluator drains them,
scores them, and writes the results to PostgreSQL. Putting a queue between the
two is what makes "quality scoring adds no latency to the user-facing response"
structurally true rather than an aspiration — the request path never waits on a
judge.

Backed by a Redis Stream with a consumer group, so an entry stays pending until
it is acknowledged. If the evaluator dies mid-batch, those outcomes are still
claimable rather than silently lost — and a lost batch reads downstream as a gap
in the quality window, which is exactly the signal that must not be fabricated.

The in-memory implementation exists so the evaluator can be tested without Redis
running, and satisfies the same contract.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from aiflags.core.models import EvaluationReason
from aiflags.sdk import Outcome

DEFAULT_STREAM = "aiflags:outcomes"
DEFAULT_GROUP = "evaluators"


@dataclass(frozen=True, slots=True)
class QueuedOutcome:
    """An outcome plus the handle needed to acknowledge it."""

    id: str
    outcome: Outcome


class OutcomeQueue(Protocol):
    """Transport between the SDK and the evaluator worker."""

    def send(self, batch: list[Outcome]) -> None: ...

    def consume(self, max_items: int = 100, block_ms: int = 1000) -> list[QueuedOutcome]: ...

    def acknowledge(self, ids: list[str]) -> int: ...

    def pending(self) -> int: ...


class InMemoryOutcomeQueue:
    """Process-local queue for tests and the offline demo.

    Mirrors the Redis semantics that matter: consumed entries stay pending until
    acknowledged, so a test can prove the evaluator does not lose a batch it
    failed to process.
    """

    def __init__(self) -> None:
        self._ready: list[QueuedOutcome] = []
        self._unacknowledged: dict[str, QueuedOutcome] = {}
        self._sequence = 0
        self._lock = threading.RLock()

    def send(self, batch: list[Outcome]) -> None:
        with self._lock:
            for outcome in batch:
                self._sequence += 1
                self._ready.append(QueuedOutcome(id=str(self._sequence), outcome=outcome))

    def consume(self, max_items: int = 100, block_ms: int = 1000) -> list[QueuedOutcome]:
        with self._lock:
            taken = self._ready[:max_items]
            self._ready = self._ready[max_items:]
            for entry in taken:
                self._unacknowledged[entry.id] = entry
            return taken

    def acknowledge(self, ids: list[str]) -> int:
        with self._lock:
            return sum(
                1 for entry_id in ids if self._unacknowledged.pop(entry_id, None)
            )

    def pending(self) -> int:
        with self._lock:
            return len(self._ready) + len(self._unacknowledged)

    def reclaim(self) -> list[QueuedOutcome]:
        """Return unacknowledged entries to the ready set."""
        with self._lock:
            reclaimed = list(self._unacknowledged.values())
            self._ready = reclaimed + self._ready
            self._unacknowledged.clear()
            return reclaimed


class RedisOutcomeQueue:
    """Redis Stream implementation with a consumer group."""

    def __init__(
        self,
        client,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        consumer: str = "evaluator-1",
        max_length: int = 100_000,
    ) -> None:
        self._client = client
        self._stream = stream
        self._group = group
        self._consumer = consumer
        # Capped so a stalled evaluator cannot exhaust memory. The cap is far
        # above any window the controller reads, so trimming only ever discards
        # outcomes already too old to influence a decision.
        self._max_length = max_length
        self._ensure_group()

    @classmethod
    def from_url(cls, url: str, **kwargs) -> RedisOutcomeQueue:
        import redis

        return cls(redis.Redis.from_url(url, decode_responses=True), **kwargs)

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - redis raises a generic ResponseError
            if "BUSYGROUP" not in str(exc):
                raise

    def send(self, batch: list[Outcome]) -> None:
        if not batch:
            return
        pipeline = self._client.pipeline()
        for outcome in batch:
            pipeline.xadd(
                self._stream,
                {"payload": json.dumps(encode_outcome(outcome))},
                maxlen=self._max_length,
                approximate=True,
            )
        pipeline.execute()

    def consume(self, max_items: int = 100, block_ms: int = 1000) -> list[QueuedOutcome]:
        response = self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=max_items,
            block=block_ms,
        )
        if not response:
            return []
        _stream, entries = response[0]
        return [
            QueuedOutcome(
                id=entry_id, outcome=decode_outcome(json.loads(fields["payload"]))
            )
            for entry_id, fields in entries
        ]

    def acknowledge(self, ids: list[str]) -> int:
        if not ids:
            return 0
        return int(self._client.xack(self._stream, self._group, *ids))

    def pending(self) -> int:
        info = self._client.xpending(self._stream, self._group)
        undelivered = self._client.xlen(self._stream)
        delivered = int(info["pending"]) if info else 0
        return max(delivered, 0) + max(undelivered - self._delivered_total(), 0)

    def _delivered_total(self) -> int:
        try:
            groups = self._client.xinfo_groups(self._stream)
        except Exception:  # noqa: BLE001
            return 0
        for group in groups:
            if group["name"] == self._group:
                return int(group.get("entries-read") or 0)
        return 0

    def clear(self) -> None:
        self._client.delete(self._stream)
        self._ensure_group()


def encode_outcome(outcome: Outcome) -> dict[str, Any]:
    return {
        "evaluation_id": outcome.evaluation_id,
        "flag_key": outcome.flag_key,
        "variant_key": outcome.variant_key,
        "reason": outcome.reason.value,
        "occurred_at": outcome.occurred_at.isoformat(),
        "output": outcome.output,
        "latency_ms": outcome.latency_ms,
        "error": outcome.error,
        "feedback": outcome.feedback,
        "is_shadow": outcome.is_shadow,
        "metadata": outcome.metadata,
    }


def decode_outcome(payload: dict[str, Any]) -> Outcome:
    return Outcome(
        evaluation_id=payload["evaluation_id"],
        flag_key=payload["flag_key"],
        variant_key=payload["variant_key"],
        reason=EvaluationReason(payload["reason"]),
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        output=payload.get("output"),
        latency_ms=payload.get("latency_ms"),
        error=payload.get("error"),
        feedback=payload.get("feedback"),
        is_shadow=payload.get("is_shadow", False),
        metadata=payload.get("metadata", {}),
    )
