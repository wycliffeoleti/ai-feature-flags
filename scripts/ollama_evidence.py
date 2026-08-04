"""Drive the rollout with a real local model as the judge.

Everything else in this project scores outputs with `FixtureJudge`, a
deterministic rubric. That makes the whole pipeline reproducible, but it means
no claim can be made that a *model* ever judged anything. This script closes
that gap using a locally running Ollama process — real model inference, no paid
API, no egress beyond loopback.

**The quality gates are identical to the fixture run.** Same 50-evaluation
sustained window, same P10 threshold of 3.0, same minimum sample count. The only
difference is the ramp: it starts at 50% rather than 1%, because a 1% stage needs
~5000 requests to accumulate 50 experimental samples and each one here is a real
inference call. Traffic volume changes; strictness does not.

Usage:

    ollama serve                       # if not already running
    PYTHONPATH=. uv run python scripts/ollama_evidence.py --model phi4-mini
"""

from __future__ import annotations

import argparse
import statistics
import time
from datetime import datetime, UTC
from pathlib import Path

from aiflags.core.models import (
    QualitySignal,
    RolloutPlan,
    Stage,
    VariantKind,
)
from aiflags.demo.generator import BROKEN_TEMPLATE, GOOD_TEMPLATE
from aiflags.demo.scenario import DEMO_POLICY, Demo
from aiflags.judge.ollama import OllamaJudge

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "OLLAMA_EVIDENCE.md"

# Same gates as the fixture run; a shorter ramp so each stage needs ~110 real
# inference calls rather than ~6000.
OLLAMA_PLAN = RolloutPlan(
    stages=(
        Stage(percentage=50.0, dwell_seconds=6 * 3600),
        Stage(percentage=100.0, dwell_seconds=24 * 3600),
    ),
    cooldown_seconds=3600.0,
)
REQUESTS_PER_STAGE = 130


def run(key: str, template: str, model: str, endpoint: str) -> dict:
    demo = Demo(
        subjects=400,
        judge=OllamaJudge(model=model, endpoint=endpoint, timeout_seconds=120),
        plan=OLLAMA_PLAN,
        policy=DEMO_POLICY,
    )
    demo.create_flag(key, template)
    demo.start_rollout(key)

    started = time.time()
    result = demo.run(key, max_ticks=4, requests=REQUESTS_PER_STAGE)
    elapsed = time.time() - started

    def scores(kind):
        return [
            s.value
            for s in demo.quality.samples(
                key, QualitySignal.JUDGE_SCORE, kind, limit=1000
            )
            if s.scored
        ]

    experimental, baseline = scores(VariantKind.EXPERIMENTAL), scores(
        VariantKind.BASELINE
    )
    unscored = [
        s
        for s in demo.quality.samples(
            key, QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL, limit=1000
        )
        if not s.scored
    ]

    return {
        "key": key,
        "template": template,
        "result": result,
        "experimental": experimental,
        "baseline": baseline,
        "unscored": len(unscored),
        "elapsed": elapsed,
    }


def describe(values: list[float]) -> str:
    if not values:
        return "no scored samples"
    ordered = sorted(values)
    p10 = ordered[max(0, int(0.10 * (len(ordered) - 1)))]
    return (
        f"n={len(values)} mean={statistics.fmean(values):.2f} "
        f"p10={p10:.2f} min={ordered[0]:.1f} max={ordered[-1]:.1f}"
    )


def report(run_data: dict) -> str:
    result = run_data["result"]
    lines = [
        f"### `{run_data['key']}`",
        "",
        f"Template: `{run_data['template']}`",
        "",
        "```",
        f"experimental : {describe(run_data['experimental'])}",
        f"baseline     : {describe(run_data['baseline'])}",
        f"unscored     : {run_data['unscored']}",
        f"controller   : {' -> '.join(result.actions) or '(none)'}",
        f"final        : {result.final_status.value} at {result.final_percentage:g}%",
    ]
    if result.rollback_reason:
        lines.append(f"reason       : {result.rollback_reason}")
    lines += [f"wall clock   : {run_data['elapsed']:.0f}s", "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="phi4-mini")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    print(f"Judging with {args.model} at {args.endpoint}")
    print("Gates are identical to the fixture run; only the ramp is shorter.\n")

    runs = [
        run("subject_line_broken_ollama", BROKEN_TEMPLATE, args.model, args.endpoint),
        run("subject_line_good_ollama", GOOD_TEMPLATE, args.model, args.endpoint),
    ]
    for data in runs:
        print(report(data))

    bad, good = runs
    ok = bad["result"].rolled_back and good["result"].fully_rolled_out

    OUTPUT.write_text(
        "\n".join(
            [
                "# Real-model judge evidence",
                "",
                f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} by",
                "`scripts/ollama_evidence.py`.",
                "",
                "Everything else in this project scores outputs with `FixtureJudge`, a",
                "deterministic rubric — reproducible, but not a model. This run drives the",
                f"same rollout machinery with **`{args.model}`** running locally under Ollama:",
                "real inference, no paid API, no egress beyond loopback.",
                "",
                "**The quality gates are identical to the fixture run** — the same",
                "50-evaluation sustained window and P10 threshold of 3.0. Only the ramp is",
                "shorter (starting at 50% rather than 1%), because a 1% stage needs roughly",
                "5000 requests to accumulate 50 experimental samples and every one here is a",
                "real inference call. Traffic volume changes; strictness does not.",
                "",
                "## Results",
                "",
                *(report(data) for data in runs),
                "## Reading this",
                "",
                "The broken variant leaks an unrendered `{customer_name}` placeholder into",
                "every subject line. The model scores those outputs materially below the",
                "clean ones, the P10 gate breaches, and the controller rolls back — the same",
                "decision path the fixture judge drives, reached from real model judgements.",
                "",
                "Scores from a language model are not deterministic. Re-running this will",
                "produce different numbers; what should reproduce is the *separation*",
                "between the two variants and the resulting decision.",
                "",
                f"**Outcome: {'PASS' if ok else 'FAIL'}**",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print("=" * 72)
    print(
        "PASS  real-model judge rolled back the broken variant and cleared the good one"
        if ok
        else "FAIL  the real-model run did not reach the expected end state"
    )
    print(f"      written to {OUTPUT.relative_to(OUTPUT.parents[1])}")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
