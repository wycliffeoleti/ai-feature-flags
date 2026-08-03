"""Deterministic subject-to-bucket assignment.

A subject's bucket is a stable point in ``[0.0, 1.0)`` derived from the subject
key, the flag key, and a per-flag salt. Rollout is then simply ``bucket <
percentage``, which gives two properties the rollout controller depends on:

* **Stickiness** — a subject's bucket never changes, so the same user keeps
  getting the same variant across restarts, redeploys, and snapshot versions.
* **Monotonic ramp** — raising the percentage only ever *adds* subjects to the
  experimental variant. Nobody is yanked back to baseline by a ramp-up, which
  would otherwise show up as spurious quality noise mid-rollout.

Including the flag key in the hash keeps flags independent: a subject who lands
in the first 1% of one flag is not thereby in the first 1% of every flag.
"""

from __future__ import annotations

import hashlib

_UINT64_RANGE = 2**64


def bucket(subject_key: str, flag_key: str, salt: str) -> float:
    """Return the stable bucket in ``[0.0, 1.0)`` for a subject under a flag.

    ``salt`` lets an operator deliberately reshuffle assignment for one flag
    (a fresh experiment on the same population) without disturbing any other
    flag.
    """
    payload = f"{flag_key}\x1f{salt}\x1f{subject_key}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") / _UINT64_RANGE


def _freeze_vectors() -> str:
    """Emit the regression-lock vectors consumed by the Phase 1 test suite."""
    import json

    cases = [
        ("user-1", "subject_line_v2", "salt-a"),
        ("user-42", "subject_line_v2", "salt-a"),
        ("user-42", "subject_line_v2", "salt-b"),
        ("user-42", "other_flag", "salt-a"),
        ("", "subject_line_v2", "salt-a"),
        ("üñïçø∂é-user", "subject_line_v2", "salt-a"),
        ("session:9f3c1a2b", "model_swap", "2026-08-04"),
    ]
    return json.dumps(
        [
            {
                "subject_key": subject,
                "flag_key": flag,
                "salt": salt,
                "bucket": bucket(subject, flag, salt),
            }
            for subject, flag, salt in cases
        ],
        indent=2,
        ensure_ascii=False,
    )


if __name__ == "__main__":  # pragma: no cover - developer utility
    import sys

    if "--freeze-vectors" in sys.argv:
        print(_freeze_vectors())
    else:
        sys.exit("usage: python -m aiflags.core.bucketing --freeze-vectors")
