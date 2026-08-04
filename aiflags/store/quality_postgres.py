"""PostgreSQL quality store.

Same contract as :class:`~aiflags.store.quality.InMemoryQualityStore`, verified
by the same test body.

Observations are written in batches because the evaluator drains the queue in
batches; one round trip per sample would make the worker the bottleneck in the
one place throughput actually matters.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from aiflags.core.models import QualitySignal, VariantKind
from aiflags.core.windows import Sample
from aiflags.store.quality import (
    DecisionRecord,
    QualityObservation,
    StoredRolloutState,
)


class PostgresQualityStore:
    """Durable implementation of :class:`~aiflags.store.quality.QualityStore`."""

    def __init__(self, dsn: str) -> None:
        self._lock = threading.RLock()
        self._connection = psycopg.connect(dsn, row_factory=dict_row)

    def close(self) -> None:
        self._connection.close()

    def reset_for_tests(self) -> None:
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "TRUNCATE quality_samples, rollout_state, controller_decisions "
                    "RESTART IDENTITY"
                )

    # -- observations -------------------------------------------------------- #

    def record_observations(self, observations: list[QualityObservation]) -> int:
        if not observations:
            return 0
        rows = [
            (
                observation.flag_key,
                observation.evaluation_id,
                observation.variant_kind.value,
                observation.signal.value,
                observation.value,
                observation.scored,
                observation.is_shadow,
                observation.reason,
                observation.occurred_at,
            )
            for observation in observations
        ]
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO quality_samples (flag_key, evaluation_id, "
                    "variant_kind, signal, value, scored, is_shadow, reason, "
                    "occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )
        return len(rows)

    def samples(
        self,
        flag_key: str,
        signal: QualitySignal,
        variant_kind: VariantKind,
        limit: int = 500,
        include_shadow: bool = False,
    ) -> list[Sample]:
        query = (
            "SELECT value, scored, occurred_at FROM quality_samples "
            "WHERE flag_key = %s AND signal = %s AND variant_kind = %s"
        )
        params: list[Any] = [flag_key, signal.value, variant_kind.value]
        if not include_shadow:
            query += " AND is_shadow = FALSE"
        # Newest first with a LIMIT, then reversed below: the alternative
        # (ordering ascending) would scan the whole history to find the tail.
        query += " ORDER BY id DESC LIMIT %s"
        params.append(limit)

        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [
            Sample(
                value=row["value"] if row["value"] is not None else 0.0,
                at=row["occurred_at"],
                scored=row["scored"],
            )
            for row in reversed(rows)
        ]

    # -- rollout state -------------------------------------------------------- #

    def get_rollout_state(self, flag_key: str) -> StoredRolloutState | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT flag_key, stage_index, stage_entered_at, rolled_back_at "
                "FROM rollout_state WHERE flag_key = %s",
                (flag_key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredRolloutState(
            flag_key=row["flag_key"],
            stage_index=row["stage_index"],
            stage_entered_at=row["stage_entered_at"],
            rolled_back_at=row["rolled_back_at"],
        )

    def save_rollout_state(self, state: StoredRolloutState) -> None:
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO rollout_state "
                    "(flag_key, stage_index, stage_entered_at, rolled_back_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (flag_key) DO UPDATE SET "
                    "stage_index = EXCLUDED.stage_index, "
                    "stage_entered_at = EXCLUDED.stage_entered_at, "
                    "rolled_back_at = EXCLUDED.rolled_back_at, "
                    "updated_at = now()",
                    (
                        state.flag_key,
                        state.stage_index,
                        state.stage_entered_at,
                        state.rolled_back_at,
                    ),
                )

    # -- decisions ------------------------------------------------------------ #

    def record_decision(self, decision: DecisionRecord) -> None:
        with self._lock, self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO controller_decisions "
                    "(flag_key, action, reason, evidence, canary, decided_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        decision.flag_key,
                        decision.action,
                        decision.reason,
                        Jsonb(decision.evidence),
                        Jsonb(decision.canary) if decision.canary else None,
                        decision.decided_at,
                    ),
                )

    def decisions(
        self, flag_key: str | None = None, limit: int = 100
    ) -> list[DecisionRecord]:
        query = (
            "SELECT flag_key, action, reason, evidence, canary, decided_at "
            "FROM controller_decisions"
        )
        params: list[Any] = []
        if flag_key is not None:
            query += " WHERE flag_key = %s"
            params.append(flag_key)
        query += " ORDER BY id DESC LIMIT %s"
        params.append(limit)

        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [
            DecisionRecord(
                flag_key=row["flag_key"],
                action=row["action"],
                reason=row["reason"],
                decided_at=row["decided_at"],
                evidence=row["evidence"],
                canary=row["canary"],
            )
            for row in reversed(rows)
        ]
