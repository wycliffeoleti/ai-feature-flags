"""The client library applications embed.

Its whole contract is defensive, because it runs inside somebody else's request
handler:

* **Never raise.** Every transport failure resolves to baseline traffic, not to
  an exception in the caller's code.
* **Never block.** :meth:`FlagClient.evaluate` reads an in-memory snapshot and
  does no I/O at all; :meth:`FlagClient.record_outcome` appends to a bounded
  buffer. Network work happens only in :meth:`FlagClient.refresh` and
  :meth:`FlagClient.flush`, which a host application runs on its own schedule.
* **Never ramp on uncertainty.** A missing, stale, or unreadable snapshot serves
  baseline.

Integration is three lines for an ordinary rollout::

    result = client.evaluate("subject_line", EvaluationContext(subject_key=user_id))
    subject = generate(prompt=result.variant.config["prompt"])
    client.record_outcome(result, output=subject, latency_ms=elapsed_ms)

Shadow mode costs one line more and a second inference call — see
:meth:`record_shadow_outcome`.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol

from aiflags.clock import Clock, SystemClock
from aiflags.core.evaluation import UNKNOWN_BASELINE, evaluate
from aiflags.core.models import (
    DEGRADED_REASONS,
    EvaluationContext,
    EvaluationReason,
    EvaluationResult,
    FlagSnapshot,
    Variant,
)

DEFAULT_BUFFER_CAPACITY = 10_000
DEFAULT_MAX_STALENESS_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Outcome:
    """One observed result of a flag-gated response, awaiting quality scoring."""

    evaluation_id: str
    flag_key: str
    variant_key: str
    reason: EvaluationReason
    occurred_at: datetime
    output: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    feedback: float | None = None
    is_shadow: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class SnapshotSource(Protocol):
    """Where the SDK reads published flag snapshots from."""

    def fetch(self) -> FlagSnapshot | None: ...


class OutcomeSink(Protocol):
    """Where the SDK hands outcomes off for asynchronous scoring."""

    def send(self, batch: list[Outcome]) -> None: ...


class FlagClient:
    """Evaluates flags locally and buffers outcomes for asynchronous scoring."""

    def __init__(
        self,
        source: SnapshotSource,
        sink: OutcomeSink,
        clock: Clock | None = None,
        max_staleness_seconds: float | None = DEFAULT_MAX_STALENESS_SECONDS,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        default_variant: Variant | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._clock = clock if clock is not None else SystemClock()
        self._max_staleness_seconds = max_staleness_seconds
        self._default_variant = default_variant
        self._snapshot: FlagSnapshot | None = None
        # A bounded deque is the whole backpressure policy: when the evaluator
        # cannot keep up, the application keeps serving and the SDK sheds the
        # oldest telemetry rather than growing without limit inside a web worker.
        self._buffer: deque[Outcome] = deque(maxlen=buffer_capacity)
        self._dropped = 0

    # -- snapshot lifecycle ------------------------------------------------- #

    @property
    def snapshot_version(self) -> int:
        return self._snapshot.version if self._snapshot is not None else 0

    def refresh(self) -> bool:
        """Pull a newer snapshot. Returns whether one was applied. Never raises.

        Older or equal versions are rejected so out-of-order delivery cannot roll
        the data plane backwards onto a percentage an operator already changed.
        """
        try:
            candidate = self._source.fetch()
        except Exception:
            return False
        if candidate is None or candidate.version <= self.snapshot_version:
            return False
        self._snapshot = candidate
        return True

    # -- evaluation --------------------------------------------------------- #

    def evaluate(
        self,
        flag_key: str,
        context: EvaluationContext,
        default_variant: Variant | None = None,
    ) -> EvaluationResult:
        """Decide which variant to serve. Does no I/O and never raises.

        ``default_variant`` replaces the sentinel baseline when no flag
        definition is available — the hook for "fall back to the non-AI code
        path".
        """
        result = evaluate(
            snapshot=self._snapshot,
            flag_key=flag_key,
            context=context,
            evaluation_id=uuid.uuid4().hex,
            now=self._clock.now(),
            max_staleness_seconds=self._max_staleness_seconds,
        )
        fallback = default_variant or self._default_variant
        if fallback is not None and result.variant == UNKNOWN_BASELINE:
            return EvaluationResult(
                flag_key=result.flag_key,
                variant=fallback,
                reason=result.reason,
                snapshot_version=result.snapshot_version,
                evaluation_id=result.evaluation_id,
                shadow_variant=result.shadow_variant,
            )
        return result

    # -- outcome reporting --------------------------------------------------- #

    def record_outcome(
        self,
        result: EvaluationResult,
        output: str | None = None,
        latency_ms: float | None = None,
        error: str | None = None,
        feedback: float | None = None,
        **metadata: Any,
    ) -> None:
        """Buffer the observed result of a flag-gated response. Never blocks."""
        self._append(
            Outcome(
                evaluation_id=result.evaluation_id,
                flag_key=result.flag_key,
                variant_key=result.variant.key,
                reason=result.reason,
                occurred_at=self._clock.now(),
                output=output,
                latency_ms=latency_ms,
                error=error,
                feedback=feedback,
                is_shadow=False,
                metadata=metadata,
            )
        )

    def record_shadow_outcome(
        self,
        result: EvaluationResult,
        output: str | None = None,
        latency_ms: float | None = None,
        error: str | None = None,
        **metadata: Any,
    ) -> None:
        """Buffer the output the shadow variant *would* have produced.

        Only valid when :attr:`EvaluationResult.shadow_variant` is set. Shadow
        outcomes are scored like any other, but the rollout controller never
        advances a stage on them — they exist to catch a catastrophic variant
        before a single user sees it.
        """
        if result.shadow_variant is None:
            raise ValueError(
                f"flag {result.flag_key!r} is not in shadow mode; "
                "record_shadow_outcome has nothing to attribute the output to"
            )
        self._append(
            Outcome(
                evaluation_id=result.evaluation_id,
                flag_key=result.flag_key,
                variant_key=result.shadow_variant.key,
                reason=result.reason,
                occurred_at=self._clock.now(),
                output=output,
                latency_ms=latency_ms,
                error=error,
                is_shadow=True,
                metadata=metadata,
            )
        )

    def flush(self) -> int:
        """Send buffered outcomes. Returns how many were accepted. Never raises.

        On failure the batch is put back at the front of the buffer so a brief
        queue outage costs no telemetry, bounded by the buffer capacity.
        """
        if not self._buffer:
            return 0
        batch = list(self._buffer)
        self._buffer.clear()
        try:
            self._sink.send(batch)
        except Exception:
            self._requeue(batch)
            return 0
        return len(batch)

    # -- introspection ------------------------------------------------------- #

    @property
    def pending_outcomes(self) -> int:
        return len(self._buffer)

    @property
    def dropped_outcomes(self) -> int:
        """Outcomes discarded because the buffer was full.

        Non-zero means the quality windows are sampling, not observing — worth
        surfacing next to any rollout decision made from that data.
        """
        return self._dropped

    def is_degraded(self, flag_key: str, context: EvaluationContext) -> bool:
        """Whether ``flag_key`` currently resolves through a fallback path."""
        return self.evaluate(flag_key, context).reason in DEGRADED_REASONS

    # -- context manager ------------------------------------------------------ #

    def __enter__(self) -> FlagClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.flush()

    # -- internals ------------------------------------------------------------ #

    def _append(self, outcome: Outcome) -> None:
        if self._buffer.maxlen is not None and len(self._buffer) == self._buffer.maxlen:
            self._dropped += 1
        self._buffer.append(outcome)

    def _requeue(self, batch: list[Outcome]) -> None:
        capacity = self._buffer.maxlen
        if capacity is not None:
            room = capacity - len(self._buffer)
            if room < len(batch):
                self._dropped += len(batch) - max(room, 0)
                batch = batch[len(batch) - max(room, 0) :] if room > 0 else []
        self._buffer.extendleft(reversed(batch))
