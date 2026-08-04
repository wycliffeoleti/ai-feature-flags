"""Storage for quality observations, rollout progress, and controller decisions.

The durable record every rollout decision is made from. Kept in PostgreSQL
rather than as Redis counters on purpose: a decision to withdraw a feature from
users has to be justifiable from data that cannot be evicted under memory
pressure, and reconstructable months later when someone asks why the ramp
stopped. Redis carries the snapshot and the outcome queue, where losing an entry
is survivable; it does not carry the evidence.

Both implementations satisfy one contract and are verified by one test body.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aiflags.core.models import QualitySignal, VariantKind
from aiflags.core.windows import Sample


@dataclass(frozen=True, slots=True)
class QualityObservation:
    """One recorded measurement of one evaluation.

    ``scored=False`` means the judge could not produce a value. The observation
    is still stored — it happened, and somebody was served that output — but it
    contributes to the unscored rate rather than to the quality statistics.
    """

    flag_key: str
    evaluation_id: str
    variant_kind: VariantKind
    signal: QualitySignal
    value: float | None
    scored: bool
    occurred_at: datetime
    is_shadow: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.scored and self.value is None:
            raise ValueError("a scored observation must carry a value")
        if not self.scored and self.value is not None:
            raise ValueError(
                "an unscored observation must not carry a value; it would move "
                "the statistics while claiming to be unmeasured"
            )

    def as_sample(self) -> Sample:
        return Sample(
            value=self.value if self.value is not None else 0.0,
            at=self.occurred_at,
            scored=self.scored,
        )


@dataclass(frozen=True, slots=True)
class StoredRolloutState:
    """Where a flag's staged rollout has reached."""

    flag_key: str
    stage_index: int
    stage_entered_at: datetime
    rolled_back_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One controller decision, including the ones that changed nothing.

    Holds are recorded too. "Why did this rollout sit at 5% for six hours" is the
    question operators actually ask, and it cannot be answered from a log that
    only records changes.
    """

    flag_key: str
    action: str
    reason: str
    decided_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)
    canary: dict[str, Any] | None = None


class QualityStore(Protocol):
    """Durable store for quality observations and rollout progress."""

    def record_observations(self, observations: list[QualityObservation]) -> int: ...

    def samples(
        self,
        flag_key: str,
        signal: QualitySignal,
        variant_kind: VariantKind,
        limit: int = 500,
        include_shadow: bool = False,
    ) -> list[Sample]: ...

    def get_rollout_state(self, flag_key: str) -> StoredRolloutState | None: ...

    def save_rollout_state(self, state: StoredRolloutState) -> None: ...

    def record_decision(self, decision: DecisionRecord) -> None: ...

    def decisions(self, flag_key: str | None = None, limit: int = 100) -> list[DecisionRecord]: ...


class InMemoryQualityStore:
    """Process-local implementation, used by the tests and the offline demo."""

    def __init__(self) -> None:
        self._observations: list[QualityObservation] = []
        self._states: dict[str, StoredRolloutState] = {}
        self._decisions: list[DecisionRecord] = []
        self._lock = threading.RLock()

    def record_observations(self, observations: list[QualityObservation]) -> int:
        with self._lock:
            self._observations.extend(observations)
            return len(observations)

    def samples(
        self,
        flag_key: str,
        signal: QualitySignal,
        variant_kind: VariantKind,
        limit: int = 500,
        include_shadow: bool = False,
    ) -> list[Sample]:
        with self._lock:
            matching = [
                observation
                for observation in self._observations
                if observation.flag_key == flag_key
                and observation.signal == signal
                and observation.variant_kind == variant_kind
                and (include_shadow or not observation.is_shadow)
            ]
        # Oldest-first within the trailing window, matching what `summarize`
        # expects: it reads the tail as the most recent samples.
        return [observation.as_sample() for observation in matching[-limit:]]

    def get_rollout_state(self, flag_key: str) -> StoredRolloutState | None:
        with self._lock:
            return self._states.get(flag_key)

    def save_rollout_state(self, state: StoredRolloutState) -> None:
        with self._lock:
            self._states[state.flag_key] = state

    def record_decision(self, decision: DecisionRecord) -> None:
        with self._lock:
            self._decisions.append(decision)

    def decisions(
        self, flag_key: str | None = None, limit: int = 100
    ) -> list[DecisionRecord]:
        with self._lock:
            matching = [
                decision
                for decision in self._decisions
                if flag_key is None or decision.flag_key == flag_key
            ]
        return matching[-limit:]


def utcnow() -> datetime:
    return datetime.now(UTC)
