"""Render the dashboard to static HTML with seeded data.

Produces a browsable artefact without needing the stack running, so the pages can
be reviewed (and screenshotted) from a checkout alone.

The data is synthetic and labelled as such. It reproduces the state the demo
reaches: one flag mid-ramp and healthy, one rolled back by a quality gate, one
blind because its judge was failing.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiflags.core.models import (
    Comparison, FlagDefinition, FlagStatus, QualityGate, QualityPolicy,
    QualitySignal, Statistic, Variant, VariantKind,
)
from aiflags.dashboard.data import build_analytics, build_overview, build_overviews
from aiflags.dashboard.render import render_analytics, render_flag_detail, render_overview
from aiflags.store.memory import InMemoryFlagRepository
from aiflags.store.quality import (
    DecisionRecord, InMemoryQualityStore, QualityObservation, StoredRolloutState,
)

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "dashboard"

POLICY = QualityPolicy(gates=(QualityGate(
    signal=QualitySignal.JUDGE_SCORE, statistic=Statistic.P10,
    comparison=Comparison.BELOW, threshold=3.0, sustained_evaluations=50,
),))


def flag(key, status, percentage):
    return FlagDefinition(
        key=key,
        baseline=Variant(key="v1", kind=VariantKind.BASELINE, config={"prompt": "a"}),
        experimental=Variant(key="v2", kind=VariantKind.EXPERIMENTAL, config={"prompt": "b"}),
        quality_policy=POLICY, status=status, rollout_percentage=percentage,
    )


def observe(quality, key, value, count, kind, scored=True, offset=0):
    quality.record_observations([
        QualityObservation(
            flag_key=key, evaluation_id=f"{key}-{kind}-{offset + i}",
            variant_kind=kind, signal=QualitySignal.JUDGE_SCORE,
            value=value if scored else None, scored=scored,
            occurred_at=EPOCH + timedelta(seconds=i),
        )
        for i in range(count)
    ])


def main() -> None:
    repo, quality = InMemoryFlagRepository(), InMemoryQualityStore()

    repo.create_flag(flag("subject_line_v2", FlagStatus.ROLLING_OUT, 25.0),
                     actor="wycliffe", reason="launching the new subject line prompt")
    repo.set_rollout_percentage("subject_line_v2", 25.0,
                                actor="rollout-controller", reason="stage 2 passed its gates")
    quality.save_rollout_state(StoredRolloutState("subject_line_v2", 2, EPOCH))
    observe(quality, "subject_line_v2", 4.6, 120, VariantKind.EXPERIMENTAL)
    observe(quality, "subject_line_v2", 4.4, 120, VariantKind.BASELINE)
    quality.record_decision(DecisionRecord(
        flag_key="subject_line_v2", action="advance",
        reason="stage 1 held 5% for its full dwell with a no-worse canary; advancing to 25%",
        decided_at=EPOCH, canary={"verdict": "no_worse", "n_experimental": 120},
    ))

    repo.create_flag(flag("summary_v3", FlagStatus.ROLLED_BACK, 0.0),
                     actor="wycliffe", reason="trying a terser summary prompt")
    observe(quality, "summary_v3", 1.4, 80, VariantKind.EXPERIMENTAL)
    observe(quality, "summary_v3", 4.5, 80, VariantKind.BASELINE)
    quality.record_decision(DecisionRecord(
        flag_key="summary_v3", action="rollback",
        reason="judge_score p10 of 1.4 is below the threshold 3.0 across 50 consecutive evaluations",
        decided_at=EPOCH + timedelta(hours=1),
        canary={"verdict": "worse", "n_experimental": 80, "p_value": 0.0001},
    ))

    repo.create_flag(flag("tagline_v2", FlagStatus.ROLLING_OUT, 5.0),
                     actor="wycliffe", reason="tagline experiment")
    observe(quality, "tagline_v2", None, 40, VariantKind.EXPERIMENTAL, scored=False)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "flags.html").write_text(
        render_overview(build_overviews(repo, quality)), encoding="utf-8")
    (OUTPUT / "analytics.html").write_text(
        render_analytics(build_analytics(repo, quality)), encoding="utf-8")
    (OUTPUT / "flag-detail.html").write_text(
        render_flag_detail(
            build_overview(repo.get_flag("summary_v3"), quality),
            quality.decisions("summary_v3"), repo.audit_events("summary_v3"),
        ), encoding="utf-8")

    for path in sorted(OUTPUT.glob("*.html")):
        print(f"wrote {path.relative_to(OUTPUT.parents[2])} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
