# SDK guide

Every example on this page is executed as a doctest by
`tests/phase4/test_sdk_docs.py`. If the API changes and this document does not,
the suite fails — so nothing here can quietly stop being true.

## Setup

A `FlagClient` needs somewhere to read flag snapshots from and somewhere to send
outcomes. In production those are Redis; here they are in-process, which changes
nothing about the API.

```python
>>> from aiflags.sdk import FlagClient, RepositorySnapshotSource
>>> from aiflags.queue import InMemoryOutcomeQueue
>>> from aiflags.store.memory import InMemoryFlagRepository
>>> from aiflags.core.models import EvaluationContext
>>>
>>> repository = InMemoryFlagRepository()
>>> client = FlagClient(
...     source=RepositorySnapshotSource(repository),
...     sink=InMemoryOutcomeQueue(),
...     max_staleness_seconds=None,
... )

```

Creating a flag is normally done through the API or the dashboard. Building one
directly keeps this page self-contained:

```python
>>> from aiflags.core.models import (
...     Comparison, FlagDefinition, FlagStatus, QualityGate, QualityPolicy,
...     QualitySignal, Statistic, Variant, VariantKind,
... )
>>> policy = QualityPolicy(gates=(QualityGate(
...     signal=QualitySignal.JUDGE_SCORE,
...     statistic=Statistic.P10,
...     comparison=Comparison.BELOW,
...     threshold=3.0,
... ),))
>>> flag = FlagDefinition(
...     key="subject_line",
...     baseline=Variant(key="v1", kind=VariantKind.BASELINE,
...                      config={"prompt": "Summarise: {body}"}),
...     experimental=Variant(key="v2", kind=VariantKind.EXPERIMENTAL,
...                          config={"prompt": "One-line subject for: {body}"}),
...     quality_policy=policy,
...     status=FlagStatus.ROLLING_OUT,
...     rollout_percentage=100.0,
... )
>>> _ = repository.create_flag(flag, actor="wycliffe", reason="launch v2")
>>> client.refresh()
True

```

## The three-line integration

```python
>>> result = client.evaluate("subject_line", EvaluationContext(subject_key="user-1"))
>>> prompt = result.variant.config["prompt"]
>>> client.record_outcome(result, output="Your March invoice", latency_ms=42.0)

```

`evaluate` does no I/O — it reads a snapshot the client already holds — and never
raises. `record_outcome` appends to a bounded buffer and returns immediately;
network work happens in `refresh()` and `flush()`, which your application
schedules.

```python
>>> client.flush()
1

```

## Assignment is sticky

The same subject always gets the same variant, so a user does not see the feature
flicker between requests, and the quality windows measure quality rather than
churn.

```python
>>> first = client.evaluate("subject_line", EvaluationContext(subject_key="user-7"))
>>> second = client.evaluate("subject_line", EvaluationContext(subject_key="user-7"))
>>> first.variant.key == second.variant.key
True

```

## Pattern 1 — AI feature with a non-AI fallback

Pass a `default_variant` and the client serves it whenever no flag definition is
available: the service is unreachable, the snapshot is stale, or the flag was
deleted. Your non-AI code path becomes the failure mode.

```python
>>> non_ai = Variant(key="template", kind=VariantKind.BASELINE,
...                  config={"strategy": "first_sentence"})
>>> result = client.evaluate(
...     "a_flag_that_does_not_exist",
...     EvaluationContext(subject_key="user-1"),
...     default_variant=non_ai,
... )
>>> result.variant.config["strategy"]
'first_sentence'
>>> result.is_degraded
True

```

`is_degraded` distinguishes "served baseline because the rollout says so" from
"served baseline because something is broken". Worth logging — a rollout that
looks quiet because every client is falling back is not a rollout going well.

## Pattern 2 — Prompt version A/B

Both variants are the same model with different prompts. The config is opaque to
the flag system, so it holds whatever your code needs.

```python
>>> result = client.evaluate("subject_line", EvaluationContext(subject_key="user-1"))
>>> sorted(result.variant.config)
['prompt']

```

## Pattern 3 — Model swap

Identical, except the config names a model. There is no separate API for this —
which is the point.

```python
>>> swap = FlagDefinition(
...     key="model_swap",
...     baseline=Variant(key="small", kind=VariantKind.BASELINE,
...                      config={"model": "llama3:8b"}),
...     experimental=Variant(key="large", kind=VariantKind.EXPERIMENTAL,
...                          config={"model": "llama3:70b"}),
...     quality_policy=policy,
...     status=FlagStatus.ROLLING_OUT,
...     rollout_percentage=100.0,
... )
>>> _ = repository.create_flag(swap, actor="wycliffe", reason="try the larger model")
>>> client.refresh()
True
>>> result = client.evaluate("model_swap", EvaluationContext(subject_key="user-1"))
>>> result.variant.config["model"]
'llama3:70b'

```

## Targeting

Rules override the percentage ramp, so you can ship to internal users first and
keep an escape hatch for specific accounts. A blocklist beats everything,
including a flag at 100%.

```python
>>> from aiflags.core.models import TargetingKind, TargetingRule
>>> targeted = FlagDefinition(
...     key="targeted",
...     baseline=Variant(key="v1", kind=VariantKind.BASELINE),
...     experimental=Variant(key="v2", kind=VariantKind.EXPERIMENTAL),
...     quality_policy=policy,
...     status=FlagStatus.FULLY_ON,
...     targeting=(
...         TargetingRule(kind=TargetingKind.BLOCKLIST,
...                       values=frozenset({"user-fragile"}),
...                       variant_kind=VariantKind.BASELINE),
...     ),
... )
>>> _ = repository.create_flag(targeted, actor="wycliffe", reason="careful rollout")
>>> client.refresh()
True
>>> client.evaluate("targeted", EvaluationContext(subject_key="user-fragile")).variant.key
'v1'
>>> client.evaluate("targeted", EvaluationContext(subject_key="user-other")).variant.key
'v2'

```

## Shadow mode — the one pattern that is not three lines

Shadow mode runs the experimental variant on all traffic without showing anyone
the result. The SDK cannot do that alone: it does not know how to run your AI
feature. So it tells you both what to serve and what to shadow, and your
application runs both.

```python
>>> _ = repository.set_status(
...     "subject_line", FlagStatus.SHADOW, actor="wycliffe", reason="dark launch"
... )
>>> client.refresh()
True
>>> result = client.evaluate("subject_line", EvaluationContext(subject_key="user-1"))
>>> result.variant.kind.value          # what the user sees
'baseline'
>>> result.shadow_variant.kind.value   # what you additionally run and report
'experimental'

```

```python
>>> client.record_outcome(result, output="Your March invoice", latency_ms=40.0)
>>> client.record_shadow_outcome(result, output="March invoice ready", latency_ms=55.0)
>>> client.flush()
2

```

This costs a second inference call per request. Shadow scores are judged like any
other, but can never advance a rollout — they only tell you whether starting one
is safe.

## Failure behaviour

The client is designed so that no failure of the flag service becomes an
exception in your request handler.

| What broke | What your code sees |
|---|---|
| Flag service unreachable | Last good snapshot keeps serving |
| Snapshot older than `max_staleness_seconds` | Baseline, `is_degraded` true |
| Redis unreachable | In-memory snapshot serves; outcomes buffer, then drop with a counter |
| Unknown flag key | Your `default_variant`, or a sentinel baseline |

A refresh against a broken source returns `False` rather than raising, and
evaluation carries on with what it already had:

```python
>>> class BrokenSource:
...     def fetch(self):
...         raise ConnectionError("flag service unreachable")
>>>
>>> offline = FlagClient(source=BrokenSource(), sink=InMemoryOutcomeQueue())
>>> offline.refresh()
False
>>> offline.evaluate("subject_line", EvaluationContext(subject_key="user-1")).reason.value
'no_snapshot'

```

Check `client.dropped_outcomes` if you care whether the quality windows are
sampling rather than observing:

```python
>>> client.dropped_outcomes
0

```
