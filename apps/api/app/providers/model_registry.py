"""
Model Registry — Passage 1 §4.5, with the operational detail Passage 4
§3.2 (G02) recovers for Grok and §4.3.1 establishes for Gemma.

Two concrete requirements implemented here that did not exist before:

1. Retired-slug redirects (G02b). Passage 4:
     "as of the 15 May 2026 retirement, requests to grok-4-1-fast,
      grok-4-fast, grok-4-0709, and grok-3 redirect to grok-4.3 and are
      billed at grok-4.3 pricing — do not hardcode a retired slug."
   `resolve_model_slug()` performs that redirect in-process and reports
   it, so a stale config value in someone's .env is corrected *and*
   surfaced, rather than silently billed at a different rate.

   Also G02a: the exact string is `grok-4.6`; the hyphenated `grok-4-6`
   form "appears in URLs only and must not be sent as the API model
   value". That's enforced as a redirect too.

2. Status automation hook (G02g). Passage 4 asks for "an admin job that
   periodically calls the models endpoint (or catches a 4xx 'model not
   found') and flips status automatically, with an alert, rather than
   relying on manual upkeep." `mark_model_retired()` is the catch-a-4xx
   half, wired into the providers; the periodic-poll half is exposed as
   an admin endpoint since this deployment has no scheduler yet.

   `grok-code-fast-1` (G02e) is listed as explicitly non-routable for
   trading analysis rather than omitted, so a future config change
   can't quietly point trading traffic at a coding model.

Provider identity vs. transport (§4.3.1) is a hard rule here: Gemma 4 is
a provider; the Gemini API is transport. No entry in this registry is
named, typed, or exposed as "Gemini".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from ..observability import get_logger

logger = get_logger("providers.registry")

ModelStatus = Literal[
    "active", "disabled", "deprecated", "retired", "local", "hosted", "fallback"
]

# Passage 1 §4.5 / §9.1: the expanded seven-value enum, explicit rather
# than inferred from other columns.
MODEL_STATUSES: tuple[ModelStatus, ...] = (
    "active",
    "disabled",
    "deprecated",
    "retired",
    "local",
    "hosted",
    "fallback",
)


@dataclass
class ModelEntry:
    provider_id: str
    model_id: str
    display_name: str
    status: ModelStatus = "active"
    context_window: int | None = None
    input_modalities: list[str] = field(default_factory=lambda: ["text"])
    output_modalities: list[str] = field(default_factory=lambda: ["text"])
    is_default: bool = False
    is_fallback: bool = False
    online_capable: bool = True
    offline_capable: bool = False
    # Set when this slug has been retired in favour of another.
    redirects_to: str | None = None
    # Models that exist but must never serve trading analysis (G02e).
    routable_for_analysis: bool = True
    notes: str = ""


# ---------------------------------------------------------------------------
# Grok / xAI — Passage 1 §4.2, Passage 4 §3.2
# ---------------------------------------------------------------------------

_GROK_MODELS: list[ModelEntry] = [
    ModelEntry(
        provider_id="grok",
        model_id="grok-4.6",
        display_name="Grok 4.6",
        status="active",
        context_window=500_000,
        is_default=True,
        notes="Current flagship, launched 12 Aug 2026. Send exactly 'grok-4.6'.",
    ),
    ModelEntry(
        provider_id="grok",
        model_id="grok-4.3",
        display_name="Grok 4.3",
        status="active",
        is_fallback=True,
        notes="Redirect target for the 15 May 2026 slug retirements; billed at 4.3 pricing.",
    ),
    # The 15 May 2026 retirements, recorded so a stale config is
    # corrected in-process instead of failing at the API.
    ModelEntry("grok", "grok-4-1-fast", "Grok 4.1 Fast (retired)", "retired",
               redirects_to="grok-4.3", notes="Retired 15 May 2026."),
    ModelEntry("grok", "grok-4-fast", "Grok 4 Fast (retired)", "retired",
               redirects_to="grok-4.3", notes="Retired 15 May 2026."),
    ModelEntry("grok", "grok-4-0709", "Grok 4 (0709, retired)", "retired",
               redirects_to="grok-4.3", notes="Retired 15 May 2026."),
    ModelEntry("grok", "grok-3", "Grok 3 (retired)", "retired",
               redirects_to="grok-4.3", notes="Retired 15 May 2026."),
    # G02a: the hyphenated form is a URL artefact, not an API value.
    ModelEntry("grok", "grok-4-6", "Grok 4.6 (URL form)", "deprecated",
               redirects_to="grok-4.6",
               notes="URL-only spelling; must not be sent as the API model value."),
    # G02e: exists, but never for trading analysis.
    ModelEntry("grok", "grok-code-fast-1", "Grok Code Fast 1", "retired",
               redirects_to="grok-build-0.1", routable_for_analysis=False,
               notes="Coding-oriented; not relevant to trading analysis. Do not route here."),
    ModelEntry("grok", "grok-build-0.1", "Grok Build 0.1", "active",
               routable_for_analysis=False,
               notes="Coding-oriented successor to grok-code-fast-1. Not for trading analysis."),
]

# ---------------------------------------------------------------------------
# Gemma 4 — Passage 1 §4.3. Provider identity is Gemma 4; the Gemini API
# is only the transport that reaches it (§4.3.1).
# ---------------------------------------------------------------------------

_GEMMA_MODELS: list[ModelEntry] = [
    ModelEntry(
        "gemma", "gemma-4-4b-it", "Gemma 4 4B (instruction-tuned)", "active",
        context_window=128_000, is_default=True,
        input_modalities=["text", "image", "audio"], offline_capable=True,
        notes="Verify the exact slug against Google's current model list before relying on it.",
    ),
    ModelEntry(
        "gemma", "gemma-4-12b-it", "Gemma 4 12B (unified multimodal)", "active",
        context_window=256_000, input_modalities=["text", "image", "audio"],
        offline_capable=True, notes="Added 3 June 2026.",
    ),
    ModelEntry(
        "gemma", "gemma-4-26b-a4b-it", "Gemma 4 26B A4B (MoE)", "active",
        context_window=256_000, input_modalities=["text", "image"], offline_capable=True,
        notes="Mixture-of-Experts. LiteRT-LM does not yet cover this size.",
    ),
    ModelEntry(
        "gemma", "gemma-4-31b-it", "Gemma 4 31B (dense)", "active",
        context_window=256_000, input_modalities=["text", "image"], offline_capable=True,
        notes="Largest dense variant; vLLM is the practical local server path.",
    ),
]

_CUSTOM_MODELS: list[ModelEntry] = [
    ModelEntry(
        "custom", "custom-configured", "Oxygen AI (configured endpoint)", "active",
        is_default=True, offline_capable=True,
        notes="Any OpenAI-compatible endpoint; the owner's choice per Passage 1 §4.1.",
    )
]

_MOCK_MODELS: list[ModelEntry] = [
    ModelEntry(
        "mock", "mock-v1", "Mock Reasoner (offline)", "active", is_default=True,
        online_capable=False, offline_capable=True,
        notes="Deterministic rule-based reasoner. Demo/testing only, never a real provider.",
    )
]


class ModelRegistry:
    """
    In-process catalogue of known models.

    The database (`ai_models`) remains the system of record for anything
    an admin creates or edits; this catalogue is the seed and the source
    of the redirect rules, so routing stays correct even before a
    database is configured.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ModelEntry] = {}
        for entry in _GROK_MODELS + _GEMMA_MODELS + _CUSTOM_MODELS + _MOCK_MODELS:
            self._entries[(entry.provider_id, entry.model_id)] = entry

    def all(self, provider_id: str | None = None) -> list[ModelEntry]:
        values = list(self._entries.values())
        if provider_id:
            values = [e for e in values if e.provider_id == provider_id]
        return sorted(values, key=lambda e: (e.provider_id, not e.is_default, e.model_id))

    def get(self, provider_id: str, model_id: str) -> ModelEntry | None:
        return self._entries.get((provider_id, model_id))

    def default_model_id(self, provider_id: str) -> str | None:
        for entry in self._entries.values():
            if entry.provider_id == provider_id and entry.is_default:
                return entry.model_id
        return None

    def fallback_model_id(self, provider_id: str) -> str | None:
        for entry in self._entries.values():
            if entry.provider_id == provider_id and entry.is_fallback:
                return entry.model_id
        return None

    def resolve_model_slug(self, provider_id: str, model_id: str) -> tuple[str, str | None]:
        """
        Resolve a requested slug to the one that should actually be sent.

        Returns (effective_slug, note). `note` is non-None when a
        redirect or substitution happened, so the caller can log it and
        the admin UI can show it — a silent redirect is exactly the
        "billed at different pricing without knowing" problem Passage 4
        warns about.

        Redirects are followed transitively with a cycle guard.
        """
        seen: set[str] = set()
        current = model_id
        notes: list[str] = []

        while True:
            entry = self.get(provider_id, current)
            if entry is None:
                # Unknown slugs pass through untouched: the registry is
                # not exhaustive and a brand-new model must not be
                # blocked by this catalogue being out of date.
                return current, ("; ".join(notes) if notes else None)

            if entry.redirects_to and entry.redirects_to not in seen:
                seen.add(current)
                notes.append(
                    f"'{current}' is {entry.status} and redirects to '{entry.redirects_to}'"
                    + (f" ({entry.notes})" if entry.notes else "")
                )
                current = entry.redirects_to
                continue

            if not entry.routable_for_analysis:
                default = self.default_model_id(provider_id)
                if default and default != current:
                    notes.append(
                        f"'{current}' is not routable for trading analysis; "
                        f"using '{default}' instead"
                    )
                    return default, "; ".join(notes)

            return current, ("; ".join(notes) if notes else None)

    def mark_model_retired(self, provider_id: str, model_id: str, reason: str) -> ModelEntry | None:
        """
        Passage 4 §3.2 (G02g), the catch-a-4xx half: when a provider
        reports "model not found", flip the registry status rather than
        waiting for someone to notice manually.
        """
        entry = self.get(provider_id, model_id)
        fallback = self.fallback_model_id(provider_id) or self.default_model_id(provider_id)
        if entry is None:
            entry = ModelEntry(
                provider_id=provider_id,
                model_id=model_id,
                display_name=model_id,
                status="retired",
                redirects_to=fallback if fallback != model_id else None,
                notes=reason,
            )
            self._entries[(provider_id, model_id)] = entry
        else:
            entry.status = "retired"
            entry.notes = reason
            if entry.redirects_to is None and fallback and fallback != model_id:
                entry.redirects_to = fallback
        logger.warning(
            "model marked retired by the registry-automation hook",
            extra={"provider": provider_id, "model": model_id, "reason": reason},
        )
        return entry


_registry: ModelRegistry | None = None


def get_model_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


def configured_model_id(provider_id: str) -> str:
    """
    The model a provider should use right now: env override if set,
    otherwise the registry default — then passed through redirect
    resolution either way, so a stale env value is corrected.
    """
    registry = get_model_registry()
    env_key = {
        "grok": "GROK_MODEL_ID",
        "gemma": "GEMMA_MODEL_ID",
        "custom": "CUSTOM_AI_MODEL_ID",
    }.get(provider_id)
    requested = os.environ.get(env_key, "").strip() if env_key else ""
    if not requested:
        requested = registry.default_model_id(provider_id) or "unset"
        if provider_id == "custom":
            # Custom AI has no vendor catalogue — its model id is
            # whatever the owner configured, and "unset" is honest.
            return os.environ.get("CUSTOM_AI_MODEL_ID", "unset").strip() or "unset"
    effective, note = registry.resolve_model_slug(provider_id, requested)
    if note:
        logger.warning(
            "model slug redirected", extra={"provider": provider_id, "detail": note}
        )
    return effective