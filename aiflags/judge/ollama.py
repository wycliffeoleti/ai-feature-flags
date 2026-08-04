"""Opt-in LLM-as-judge backed by a local Ollama process.

Off by default. When enabled it gives the project real model-scored quality
evidence at no cost, since Ollama runs locally.

**The endpoint is constrained to loopback and this is enforced in the
constructor, not documented as a convention.** A judge takes the application's
output text and posts it somewhere; if that destination were configurable to an
arbitrary host, this class would be a data exfiltration path wearing a quality
-monitoring hat. Refusing anything but loopback at construction means a
misconfiguration fails immediately and loudly rather than quietly shipping user
content off the machine.

Every failure — unreachable, timeout, malformed reply, a model that answers in
prose — degrades to an unscored verdict. It never raises into the evaluator and
never invents a number.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from aiflags.judge.base import MAX_SCORE, MIN_SCORE, JudgeVerdict, clamp

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_FIRST_NUMBER = re.compile(r"\d+(?:\.\d+)?")

DEFAULT_PROMPT = """\
Rate the quality of this generated email subject line on a scale of 1 to 5.

5 = excellent: clear, specific, appropriately concise
3 = acceptable: understandable but unremarkable
1 = unusable: empty, garbled, or contains unrendered template text

Reply with a single digit from 1 to 5 and nothing else.

Subject line:
{output}
"""


class OllamaJudge:
    """Scores output with a locally running Ollama model."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434",
        model: str = "llama3",
        timeout_seconds: float = 20.0,
        prompt_template: str = DEFAULT_PROMPT,
    ) -> None:
        _require_loopback(endpoint)
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._prompt_template = prompt_template

    def score(
        self, output: str | None, context: dict[str, Any] | None = None
    ) -> JudgeVerdict:
        if output is None:
            return JudgeVerdict.unscored("no output was recorded for this evaluation")
        try:
            reply = self._generate(self._prompt_template.format(output=output))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return JudgeVerdict.unscored(f"judge unreachable: {type(exc).__name__}")
        except (json.JSONDecodeError, KeyError) as exc:
            return JudgeVerdict.unscored(f"judge returned unparseable data: {exc}")
        return self._verdict_from_reply(reply)

    def _generate(self, prompt: str) -> str:
        payload = json.dumps(
            {"model": self._model, "prompt": prompt, "stream": False}
        ).encode()
        request = urllib.request.Request(
            f"{self._endpoint}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read())["response"]

    def _verdict_from_reply(self, reply: str) -> JudgeVerdict:
        """Parse the model's answer, declining to score if it did not give one."""
        match = _FIRST_NUMBER.search(reply or "")
        if match is None:
            return JudgeVerdict.unscored(
                f"judge did not return a number: {reply[:80]!r}"
            )
        value = clamp(float(match.group()))
        return JudgeVerdict.scored_at(
            value, f"{self._model} rated {match.group()} (clamped to {value:g})"
        )


def _require_loopback(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"judge endpoint must be http(s), got {endpoint!r}")
    if (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"judge endpoint {endpoint!r} is not loopback. This judge sends "
            "application output to the endpoint, so it is restricted to a local "
            "process; a remote host would make it an exfiltration path."
        )
