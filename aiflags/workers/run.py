"""Worker entrypoints for the Compose stack.

Two long-running processes, selected by argument:

* ``evaluator`` drains the outcome queue, scores outputs, and records
  observations.
* ``controller`` ticks the rollout controller and publishes the resulting
  snapshot.

They are separate processes rather than threads in the API because that is the
claim the architecture makes: scoring and deciding happen off the request path.
Running them in-process would leave the claim untested.

Both loop until interrupted and both survive a transient store failure — the
whole point of a background worker is that it keeps going.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from aiflags.clock import SystemClock
from aiflags.judge.fixture import FixtureJudge
from aiflags.notify.recording import RecordingNotifier
from aiflags.queue import RedisOutcomeQueue
from aiflags.store.postgres import PostgresFlagRepository
from aiflags.store.quality_postgres import PostgresQualityStore
from aiflags.store.redis_snapshot import RedisSnapshotStore
from aiflags.workers.controller import RolloutController
from aiflags.workers.evaluator import QualityEvaluator

logger = logging.getLogger("aiflags.worker")

DEFAULT_CONTROLLER_INTERVAL = 30.0
DEFAULT_PUBLISH_INTERVAL = 5.0


class Shutdown:
    """Turns SIGTERM/SIGINT into a flag the loops check between iterations.

    Without this, `docker compose down` kills a worker mid-batch and those
    outcomes are redelivered — correct, but noisy. Finishing the current
    iteration first keeps shutdown clean.
    """

    def __init__(self) -> None:
        self.requested = False
        signal.signal(signal.SIGTERM, self._request)
        signal.signal(signal.SIGINT, self._request)

    def _request(self, *_args) -> None:
        logger.info("shutdown requested; finishing the current iteration")
        self.requested = True


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} must be set")
    return value


def run_evaluator() -> None:
    dsn = _require("AIFLAGS_POSTGRES_DSN")
    redis_url = _require("AIFLAGS_REDIS_URL")

    repository = PostgresFlagRepository(dsn)
    store = PostgresQualityStore(dsn)
    queue = RedisOutcomeQueue.from_url(redis_url)
    evaluator = QualityEvaluator(
        queue=queue,
        store=store,
        judge=FixtureJudge(),
        flag_lookup=repository.get_flag,
        clock=SystemClock(),
    )

    shutdown = Shutdown()
    logger.info("evaluator started")
    while not shutdown.requested:
        result = evaluator.run_once(max_items=200, block_ms=1000)
        if result.observations:
            logger.info(
                "scored %d outcomes into %d observations",
                result.consumed,
                result.observations,
            )
    logger.info("evaluator stopped")


def run_controller() -> None:
    dsn = _require("AIFLAGS_POSTGRES_DSN")
    redis_url = _require("AIFLAGS_REDIS_URL")
    interval = float(
        os.environ.get(
            "AIFLAGS_CONTROLLER_INTERVAL_SECONDS", DEFAULT_CONTROLLER_INTERVAL
        )
    )

    repository = PostgresFlagRepository(dsn)
    store = PostgresQualityStore(dsn)
    publisher = RedisSnapshotStore.from_url(redis_url)
    controller = RolloutController(
        repository=repository,
        quality_store=store,
        notifier=RecordingNotifier(),
        clock=SystemClock(),
        snapshot_publisher=publisher,
    )

    shutdown = Shutdown()
    logger.info("controller started, ticking every %.0fs", interval)
    while not shutdown.requested:
        try:
            # Publish on every tick, not only when something changed: it is what
            # heals a data plane whose Redis was flushed or evicted.
            publisher.publish(repository.snapshot())
            result = controller.tick()
            for key, decision in result.decisions.items():
                if decision.action.value != "hold":
                    logger.info(
                        "%s: %s — %s", key, decision.action.value.upper(), decision.reason
                    )
        except Exception:
            # A transient database or Redis failure must not end the process; a
            # controller that exits on the first blip cannot roll anything back.
            logger.exception("controller tick failed; retrying next interval")

        for _ in range(int(interval * 10)):
            if shutdown.requested:
                break
            time.sleep(0.1)
    logger.info("controller stopped")


COMMANDS = {"evaluator": run_evaluator, "controller": run_controller}


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: python -m aiflags.workers.run {{{'|'.join(COMMANDS)}}}")
        return 2
    COMMANDS[argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
