"""
Normalisation and defensive parsing of provider output.

Defect this fixes, confirmed by direct test before this pass: provider
JSON went straight into `ProviderReasoning` and then into a Pydantic
`TradeAnalysis` with `direction: Literal["LONG", "SHORT"]`. A model
replying `"long"` (lowercase) or `"BUY"` — which real LLMs do constantly
— raised a `ValidationError` deep inside `build_trade_analysis`. In
single mode that surfaced as a misleading 503 "Provider call failed"; in
comparison mode the slot's `reason` was a raw Pydantic traceback string
shown to the user. Measured: `"long"`, `"BUY"` and an out-of-range
confidence all failed; only exact `"LONG"`/`"SHORT"` worked.

For a platform whose whole premise is reasoning over free-text LLM JSON,
tolerating ordinary formatting variance is a correctness requirement,
not a nicety. What is *not* tolerated: inventing a direction the model
did not express. An unrecognised value becomes None (no setup), never a
guess — Passage 1 §6's "null field, not a fabricated number" rule
applies to parsing as much as to computation.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from ..observability import get_logger

logger = get_logger("providers.normalize")

Direction = Literal["LONG", "SHORT"] | None

# Only unambiguous synonyms. "neutral"/"hold"/"wait" deliberately map to
# None (no setup) rather than to a direction.
_LONG_TOKENS = {"long", "buy", "bullish", "bull", "up", "upside", "call"}
_SHORT_TOKENS = {"short", "sell", "bearish", "bear", "down", "downside", "put"}
_NULL_TOKENS = {"", "none", "null", "n/a", "na", "neutral", "hold", "wait", "flat", "no_setup"}


def normalize_direction(value: Any) -> Direction:
    """Map a provider's direction field to LONG/SHORT/None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in _NULL_TOKENS:
        return None
    if text in _LONG_TOKENS:
        return "LONG"
    if text in _SHORT_TOKENS:
        return "SHORT"
    logger.info("unrecognised direction from provider; treating as no setup",
                extra={"raw": str(value)[:64]})
    return None


def normalize_confidence(value: Any) -> float | None:
    """
    Coerce confidence onto the 0-100 scale.

    A value in [0, 1] is read as a fraction and scaled — models return
    0.85 as often as 85. The boundary value 1 is treated the same as the
    rest of that range (i.e. as 100%, the top of the fraction), rather
    than carved out as a special case: a model emitting a bare `1` for
    confidence overwhelmingly means "fully confident, expressed as a
    fraction of 1," matching the same 0-1 convention as 0.85 or 0.5.
    Treating the whole range uniformly is also simpler to reason about
    than a boundary special-case would be, and a future maintainer
    reading this function once is enough to predict its behavior on any
    input in range.

    Out-of-range values are clamped, not rejected. Rejecting them threw
    away an otherwise-valid analysis over a cosmetic field.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").strip()
        if not cleaned or cleaned.lower() in _NULL_TOKENS:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def normalize_evidence(value: Any) -> list[str]:
    """Accept a list, a newline/bullet-delimited string, or nothing."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n;]+", value)
        return [p.strip(" -•\t") for p in parts if p.strip(" -•\t")]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            text = item if isinstance(item, str) else json.dumps(item, default=str)
            text = text.strip()
            if text:
                out.append(text)
        return out
    return [str(value)]


def normalize_summary(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip() or fallback
    return json.dumps(value, default=str)


def extract_json_object(text: str) -> dict[str, Any]:
    """
    Pull the first complete JSON object out of a model response.

    The previous implementation used `re.search(r"\\{[\\s\\S]*\\}")`,
    which is greedy: given prose containing two JSON-ish blocks it
    matched from the first `{` to the *last* `}`, producing invalid
    JSON. This brace-counting scan returns the first genuinely balanced
    object, and skips braces inside string literals.
    """
    if not text:
        raise ValueError("Provider returned an empty response")

    cleaned = text.strip()
    # Strip markdown fences even though the prompt forbids them —
    # models add them anyway.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = cleaned[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
                start = -1
    raise ValueError("Provider response did not contain a parseable JSON object")


def parse_reasoning_payload(text: str) -> dict[str, Any]:
    """
    Parse and normalise a provider's raw text into the canonical fields.

    Key lookup is case- and separator-insensitive, so `reasoningSummary`,
    `reasoning_summary` and `Reasoning Summary` all resolve.
    """
    raw = extract_json_object(text)
    lookup = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in raw.items()}

    def pick(*names: str) -> Any:
        for name in names:
            key = re.sub(r"[^a-z0-9]", "", name.lower())
            if key in lookup:
                return lookup[key]
        return None

    return {
        "direction": normalize_direction(pick("direction", "signal", "bias", "action", "side")),
        "confidence": normalize_confidence(pick("confidence", "confidenceScore", "conviction")),
        "reasoning_summary": normalize_summary(
            pick("reasoningSummary", "reasoning", "summary", "analysis", "rationale"),
            fallback="The provider returned no reasoning summary.",
        ),
        "supporting_evidence": normalize_evidence(
            pick("supportingEvidence", "evidence", "supporting", "keyEvidence")
        ),
        "contradicting_evidence": normalize_evidence(
            pick("contradictingEvidence", "contradicting", "risks", "counterEvidence")
        ),
    }