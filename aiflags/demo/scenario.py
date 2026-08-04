"""End-to-end demo scenario.

Wires the whole system together and drives it through the lifecycle the guide
asks to see: a flag at 0%, a gradual ramp with quality monitoring, a deliberately
bad variant detected and rolled back automatically, and a good variant reaching
100%.

Runs on a `FakeClock`, so the guide's real schedule — 1% for two hours, 5% for
six, and so on — completes in seconds without any stage duration being shortened.
The plan under test is the plan that would ship.

Everything is deterministic: fixed emails, a fixed generator, a fixed judge. The
same run produces the same rollback for the same reason every time, which is what
makes it usable as a demo and as an integration test.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiflags.clock import FakeClock
from aiflags.core.decision import Action
from aiflags.core.models import (
    Comparison,
    EvaluationContext,
    FlagDefinition,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    RolloutPlan,
    Stage,
    Statistic,
    Variant,
    VariantKind,
)
from aiflags.demo.generator import (
    BROKEN_TEMPLATE,
    EMAILS,
    GOOD_TEMPLATE,
    SubjectLineGenerator,
)
from aiflags.judge.fixture import FixtureJudge
from aiflags.notify.recording import RecordingNotifier
from aiflags.queue import InMemoryOutcomeQueue
from aiflags.sdk import FlagClient, RepositorySnapshotSource
from aiflags.store.memory import InMemoryFlagRepository
from aiflags.store.quality import InMemoryQualityStore
from aiflags.workers.controller import RolloutController
from aiflags.workers.evaluator import QualityEvaluator

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

REQUESTS_PER_STAGE = 6000
"""Traffic per stage, sized so a 1% stage actually produces evidence.

The first stage sends 1% of traffic to the experimental variant, and the quality
gate needs 50 consecutive evaluations before it will fire. Fewer than ~5000
requests per stage and the controller correctly refuses to act at all — it holds
on thin data rather than ramping. That is the right behaviour and it is asserted
elsewhere; here it just means the demo has to send enough traffic for a 1% slice
to be measurable.
"""

DEMO_PLAN = RolloutPlan(
    stages=(
        Stage(percentage=1.0, dwell_seconds=2 * 3600),
        Stage(percentage=5.0, dwell_seconds=6 * 3600),
        Stage(percentage=25.0, dwell_seconds=24 * 3600),
        Stage(percentage=100.0, dwell_seconds=24 * 3600),
    ),
    cooldown_seconds=3600.0,
)

DEMO_POLICY = QualityPolicy(
    gates=(
        QualityGate(
            signal=QualitySignal.JUDGE_SCORE,
            statistic=Statistic.P10,
            comparison=Comparison.BELOW,
            threshold=3.0,
            sustained_evaluations=50,
        ),
        QualityGate(
            signal=QualitySignal.UNSCORED_RATE,
            statistic=Statistic.RATE,
            comparison=Comparison.ABOVE,
            threshold=0.25,
            sustained_evaluations=50,
        ),
    ),
    minimum_samples=50,
)


@dataclass
class ScenarioResult:
    """What one rollout scenario ended up doing."""

    flag_key: str
    final_status: FlagStatus
    final_percentage: float
    actions: list[str] = field(default_factory=list)
    rollback_reason: str | None = None
    notifications: int = 0
    requests: int = 0

    @property
    def rolled_back(self) -> bool:
        return self.final_status is FlagStatus.ROLLED_BACK

    @property
    def fully_rolled_out(self) -> bool:
        return self.final_status is FlagStatus.FULLY_ON


class Demo:
    """The whole stack, in one process, on a controllable clock."""

    def __init__(self, subjects: int = 5000) -> None:
        self.clock = FakeClock(EPOCH)
        self.repository = InMemoryFlagRepository(clock=self.clock)
        self.quality = InMemoryQualityStore()
        self.queue = InMemoryOutcomeQueue()
        self.notifier = RecordingNotifier()
        self.generator = SubjectLineGenerator()

        self.client = FlagClient(
            source=RepositorySnapshotSource(self.repository),
            sink=self.queue,
            clock=self.clock,
            max_staleness_seconds=None,
        )
        self.evaluator = QualityEvaluator(
            queue=self.queue,
            store=self.quality,
            judge=FixtureJudge(),
            flag_lookup=self.repository.get_flag,
            clock=self.clock,
        )
        self.controller = RolloutController(
            repository=self.repository,
            quality_store=self.quality,
            notifier=self.notifier,
            clock=self.clock,
        )
        self._subjects = [f"user-{i}" for i in range(subjects)]
        self._emails = itertools.cycle(EMAILS)

    # -- setup --------------------------------------------------------------- #

    def create_flag(self, key: str, template: str) -> None:
        """Create a flag at 0%, as an operator would before starting a rollout."""
        self.repository.create_flag(
            FlagDefinition(
                key=key,
                baseline=Variant(
                    key="v1",
                    kind=VariantKind.BASELINE,
                    config={"template": GOOD_TEMPLATE},
                ),
                experimental=Variant(
                    key="v2", kind=VariantKind.EXPERIMENTAL, config={"template": template}
                ),
                quality_policy=DEMO_POLICY,
                rollout_plan=DEMO_PLAN,
                status=FlagStatus.OFF,
                rollout_percentage=0.0,
            ),
            actor="wycliffe",
            reason=f"prepare rollout of {key}",
        )

    def start_rollout(self, key: str) -> None:
        self.repository.set_status(
            key,
            FlagStatus.ROLLING_OUT,
            actor="wycliffe",
            reason="begin staged rollout at 1%",
        )
        self.repository.set_rollout_percentage(
            key, DEMO_PLAN.stages[0].percentage, actor="wycliffe", reason="stage 1"
        )

    # -- traffic ------------------------------------------------------------- #

    def serve(self, key: str, requests: int = REQUESTS_PER_STAGE) -> int:
        """Run application traffic through the SDK, exactly as an app would."""
        self.client.refresh()
        for index in range(requests):
            subject_key = self._subjects[index % len(self._subjects)]
            email = next(self._emails)

            result = self.client.evaluate(
                key, EvaluationContext(subject_key=subject_key)
            )
            output = self.generator.generate(email, result.variant.config)
            self.client.record_outcome(result, output=output, latency_ms=35.0)

            if result.shadow_variant is not None:
                shadow_output = self.generator.generate(
                    email, result.shadow_variant.config
                )
                self.client.record_shadow_outcome(
                    result, output=shadow_output, latency_ms=48.0
                )

        self.client.flush()
        return requests

    def score(self) -> None:
        """Drain the queue through the judge until it is empty."""
        while self.evaluator.run_once(max_items=500, block_ms=0).consumed:
            pass

    # -- the loop ------------------------------------------------------------ #

    def run(
        self, key: str, max_ticks: int = 12, requests: int = REQUESTS_PER_STAGE
    ) -> ScenarioResult:
        """Serve traffic, score it, and tick the controller until terminal."""
        result = ScenarioResult(
            flag_key=key,
            final_status=FlagStatus.OFF,
            final_percentage=0.0,
        )

        for _ in range(max_ticks):
            result.requests += self.serve(key, requests)
            self.score()

            tick = self.controller.tick()
            decision = tick.decisions.get(key)
            if decision is not None:
                result.actions.append(decision.action.value)
                if decision.action is Action.ROLLBACK:
                    result.rollback_reason = decision.reason

            flag = self.repository.get_flag(key)
            if flag.status in (FlagStatus.ROLLED_BACK, FlagStatus.FULLY_ON):
                break

            # Advance past the current stage's dwell so the next tick can act.
            state = self.quality.get_rollout_state(key)
            stage_index = min(state.stage_index, len(DEMO_PLAN.stages) - 1)
            self.clock.advance(DEMO_PLAN.stages[stage_index].dwell_seconds)

        flag = self.repository.get_flag(key)
        result.final_status = flag.status
        result.final_percentage = flag.rollout_percentage
        result.notifications = len(self.notifier.sent)
        return result


def run_bad_variant() -> ScenarioResult:
    """A prompt referencing a field the pipeline does not populate."""
    demo = Demo()
    demo.create_flag("subject_line_broken", BROKEN_TEMPLATE)
    demo.start_rollout("subject_line_broken")
    return demo.run("subject_line_broken")


def run_good_variant() -> ScenarioResult:
    """A prompt that renders correctly and holds up under the quality gates."""
    demo = Demo()
    demo.create_flag("subject_line_v2", GOOD_TEMPLATE)
    demo.start_rollout("subject_line_v2")
    return demo.run("subject_line_v2")


def main() -> int:
    """Run both scenarios and report. Exit code 0 only if both behaved."""
    print("=" * 72)
    print("BASWE Project 12 — AI feature flag rollout demo")
    print("Synthetic data, deterministic judge, no network, no paid API.")
    print("=" * 72)

    print("\n[1/2] Rolling out a BROKEN prompt variant")
    print(f"      template: {BROKEN_TEMPLATE!r}")
    print("      {customer_name} is never populated, so it leaks to the user.\n")
    bad = _report(run_bad_variant())

    print("\n[2/2] Rolling out a GOOD prompt variant")
    print(f"      template: {GOOD_TEMPLATE!r}\n")
    good = _report(run_good_variant())

    ok = bad.rolled_back and good.fully_rolled_out
    print("\n" + "=" * 72)
    if ok:
        print("PASS  bad variant auto-rolled back; good variant reached 100%")
    else:
        print("FAIL  the scenario did not reach the expected end state")
    print("=" * 72)
    return 0 if ok else 1


def _report(result: ScenarioResult) -> ScenarioResult:
    print(f"      requests served : {result.requests}")
    print(f"      controller       : {' -> '.join(result.actions) or '(none)'}")
    print(f"      final status     : {result.final_status.value} "
          f"at {result.final_percentage:g}%")
    if result.rollback_reason:
        print(f"      rollback reason  : {result.rollback_reason}")
    print(f"      alerts recorded  : {result.notifications}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
