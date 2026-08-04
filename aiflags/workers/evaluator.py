"""Quality evaluator worker.

Drains the outcome queue, scores each output, and writes observations to the
durable store. This is the process that keeps judging off the request path: the
application called ``record_outcome`` and moved on; everything expensive happens
here.

Two ordering rules carry correctness:

* **Persist, then acknowledge.** If the store write fails the batch is left
  unacknowledged and will be redelivered. Acknowledging first would turn a
  database blip into a permanent hole in the quality window, and a hole reads
  downstream as "nothing bad observed".
* **A judge failure is recorded, not skipped.** An outcome whose judge timed out
  produces an *unscored* observation rather than no observation. Skipping it
  would make a broken judge indistinguishable from a healthy quiet period.

The unscored rate is deliberately not stored as its own signal. It is a property
of how the judge-score samples were judged, and :func:`summarize` already derives
it from their ``scored`` flags. Writing a second row per sample would duplicate
the same fact in two places that could then disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from aiflags.clock import Clock, SystemClock
from aiflags.core.models import (
    DEGRADED_REASONS,
    FlagDefinition,
    QualitySignal,
    VariantKind,
)
from aiflags.judge.base import QualityJudge
from aiflags.queue import OutcomeQueue, QueuedOutcome
from aiflags.sdk import Outcome
from aiflags.store.quality import QualityObservation, QualityStore

logger = logging.getLogger(__name__)

FlagLookup = Callable[[str], FlagDefinition | None]


@dataclass(frozen=True, slots=True)
class EvaluatorResult:
    """What one drain of the queue accomplished."""

    consumed: int = 0
    observations: int = 0
    acknowledged: int = 0
    skipped: int = 0


class QualityEvaluator:
    """Scores queued outcomes and records the results."""

    def __init__(
        self,
        queue: OutcomeQueue,
        store: QualityStore,
        judge: QualityJudge,
        flag_lookup: FlagLookup,
        clock: Clock | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self._judge = judge
        self._flag_lookup = flag_lookup
        self._clock = clock if clock is not None else SystemClock()

    def run_once(self, max_items: int = 100, block_ms: int = 1000) -> EvaluatorResult:
        """Drain up to ``max_items`` outcomes. Never raises."""
        try:
            entries = self._queue.consume(max_items=max_items, block_ms=block_ms)
        except Exception:
            logger.exception("failed to consume from the outcome queue")
            return EvaluatorResult()

        if not entries:
            return EvaluatorResult()

        observations: list[QualityObservation] = []
        acknowledgeable: list[str] = []
        skipped = 0

        for entry in entries:
            produced = self._observe(entry)
            if produced is None:
                # Nothing can be attributed to a flag we cannot resolve. Ack it
                # anyway: redelivering it forever would stall the queue behind a
                # message that can never be processed.
                skipped += 1
                acknowledgeable.append(entry.id)
                continue
            observations.extend(produced)
            acknowledgeable.append(entry.id)

        try:
            written = self._store.record_observations(observations)
        except Exception:
            # Leave the batch unacknowledged so it is redelivered. A lost batch
            # would show up as a gap in the window, which reads as "no problems".
            logger.exception(
                "failed to persist %d observations; leaving %d outcomes "
                "unacknowledged for redelivery",
                len(observations),
                len(acknowledgeable),
            )
            return EvaluatorResult(consumed=len(entries), skipped=skipped)

        acknowledged = self._queue.acknowledge(acknowledgeable)
        return EvaluatorResult(
            consumed=len(entries),
            observations=written,
            acknowledged=acknowledged,
            skipped=skipped,
        )

    def _observe(self, entry: QueuedOutcome) -> list[QualityObservation] | None:
        """Turn one outcome into observations, or ``None`` if unattributable."""
        outcome = entry.outcome
        flag = self._flag_lookup(outcome.flag_key)
        if flag is None:
            return None

        variant_kind = _variant_kind(flag, outcome)
        if variant_kind is None:
            # The variant key is not one this flag currently defines — the flag
            # was edited after the evaluation. Attributing the sample to either
            # side would corrupt the comparison it feeds.
            logger.warning(
                "outcome %s names variant %r which flag %r no longer defines",
                outcome.evaluation_id,
                outcome.variant_key,
                outcome.flag_key,
            )
            return None

        observations = [self._judge_observation(outcome, variant_kind)]

        if outcome.latency_ms is not None:
            observations.append(
                self._observation(
                    outcome, variant_kind, QualitySignal.LATENCY_MS, outcome.latency_ms
                )
            )

        observations.append(
            self._observation(
                outcome,
                variant_kind,
                QualitySignal.ERROR_RATE,
                1.0 if outcome.error else 0.0,
            )
        )

        if outcome.feedback is not None:
            observations.append(
                self._observation(
                    outcome, variant_kind, QualitySignal.FEEDBACK, outcome.feedback
                )
            )

        return observations

    def _judge_observation(
        self, outcome: Outcome, variant_kind: VariantKind
    ) -> QualityObservation:
        """Score the output, recording an unscored observation on any failure."""
        try:
            verdict = self._judge.score(outcome.output)
        except Exception as exc:  # noqa: BLE001 - any judge failure is unscored
            logger.warning("judge raised for %s: %s", outcome.evaluation_id, exc)
            verdict = None

        if verdict is None:
            return self._observation(
                outcome,
                variant_kind,
                QualitySignal.JUDGE_SCORE,
                None,
                scored=False,
                reason="judge raised an exception",
            )

        return self._observation(
            outcome,
            variant_kind,
            QualitySignal.JUDGE_SCORE,
            verdict.score,
            scored=verdict.scored,
            reason=verdict.reason,
        )

    def _observation(
        self,
        outcome: Outcome,
        variant_kind: VariantKind,
        signal: QualitySignal,
        value: float | None,
        scored: bool = True,
        reason: str | None = None,
    ) -> QualityObservation:
        return QualityObservation(
            flag_key=outcome.flag_key,
            evaluation_id=outcome.evaluation_id,
            variant_kind=variant_kind,
            signal=signal,
            value=value,
            scored=scored,
            occurred_at=outcome.occurred_at,
            is_shadow=outcome.is_shadow,
            reason=reason,
        )


def _variant_kind(flag: FlagDefinition, outcome: Outcome) -> VariantKind | None:
    """Attribute an outcome to the side of the rollout it came from.

    A degraded evaluation — served baseline because the snapshot was stale — is
    genuinely baseline output and counts as such. What cannot be attributed is an
    outcome naming a variant the flag no longer defines.
    """
    if outcome.variant_key == flag.experimental.key:
        return VariantKind.EXPERIMENTAL
    if outcome.variant_key == flag.baseline.key:
        return VariantKind.BASELINE
    if outcome.reason in DEGRADED_REASONS:
        # The SDK served its sentinel fallback rather than a defined variant.
        return VariantKind.BASELINE
    return None
