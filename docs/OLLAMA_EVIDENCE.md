# Real-model judge evidence

Generated 2026-08-04T05:58:24+00:00 by
`scripts/ollama_evidence.py`.

Everything else in this project scores outputs with `FixtureJudge`, a
deterministic rubric — reproducible, but not a model. This run drives the
same rollout machinery with **`phi4-mini`** running locally under Ollama:
real inference, no paid API, no egress beyond loopback.

**The quality gates are identical to the fixture run** — the same
50-evaluation sustained window and P10 threshold of 3.0. Only the ramp is
shorter (starting at 50% rather than 1%), because a 1% stage needs roughly
5000 requests to accumulate 50 experimental samples and every one here is a
real inference call. Traffic volume changes; strictness does not.

## Results

### `subject_line_broken_ollama`

Template: `Hi {customer_name}, about your {topic}`

```
experimental : n=72 mean=2.56 p10=2.00 min=2.0 max=4.0
baseline     : n=58 mean=4.03 p10=4.00 min=4.0 max=5.0
unscored     : 0
controller   : rollback
final        : rolled_back at 0%
reason       : judge_score p10 of 2 is below the threshold 3 across 50 consecutive evaluations
wall clock   : 177s
```

### `subject_line_good_ollama`

Template: `{topic} — action needed`

```
experimental : n=252 mean=4.06 p10=4.00 min=4.0 max=5.0
baseline     : n=138 mean=4.04 p10=4.00 min=4.0 max=5.0
unscored     : 0
controller   : hold -> advance -> complete
final        : fully_on at 100%
wall clock   : 298s
```

## Reading this

The broken variant leaks an unrendered `{customer_name}` placeholder into
every subject line. The model scores those outputs materially below the
clean ones, the P10 gate breaches, and the controller rolls back — the same
decision path the fixture judge drives, reached from real model judgements.

Scores from a language model are not deterministic. Re-running this will
produce different numbers; what should reproduce is the *separation* between the
two variants and the resulting decision.

**The judge is not reliable, and that is measured rather than assumed.** Over 32
repeats per variant on these same outputs, `phi4-mini` misses roughly one broken
output in four (2.0 ×25, 4.0 ×7) while never misfiring on the baseline
(4.0 ×31, 5.0 ×1). The P10 gate works precisely because the errors run in one
direction. On differently-phrased clean text the same model false-alarms about
19% of the time, and under that error rate P10 stops discriminating altogether.
See [D15](DECISIONS.md) — the statistic a gate uses has to be chosen against the
judge's measured error profile.

**Outcome: PASS**
