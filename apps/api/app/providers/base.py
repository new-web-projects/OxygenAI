"""
The one interface every provider implements — Passage 1 §4's
"provider-agnostic AI layer, now proven at three".

Extended this pass, additively (nothing existing was removed):
  * `contradicting_evidence` — Passage 4 §3.6's comparison-UI field
    list names "supporting_evidence / contradicting_evidence" as one
    field pair. Only the supporting half existed before.
  * `health_check()` — Passage 1 §4.5's router shape begins with a
    health check ("if not provider.health_ok()"). There was no such
    method on the interface at all.
  * `supports_tools` / `supports_local_runtime` — capability flags the
    Provider Router and the Admin Panel both need. Passage 1 §4.3
    confirms Gemma 4's native function calling; §4.4 records that Grok
    has no verified offline path, which is why the Admin Panel must
    render "Unavailable - hosted API only" for it rather than an
    enabled toggle.

`AnalysisContext` gains the deterministic engine's regime, structure and
risk output so a provider reasons over the same bundle the engine
computed - still never computing a number itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..schemas import IndicatorBundle

SourceTag = Literal["mock", "hosted_api", "local_model", "cache", "fallback"]


class ProviderNotConfiguredError(RuntimeError):
    """
    The provider has no usable credentials/endpoint. This is a configuration
    state, not a provider failure: it must never be counted by the circuit
    breaker (before P2 it was, so a few comparisons that merely included an
    unconfigured Grok reported it as `circuit_open` instead of
    `not_configured`, hiding the real, fixable cause).
    """

    def __init__(self, provider_id: str, missing: list[str]) -> None:
        self.provider_id = provider_id
        self.missing = list(missing)
        detail = ", ".join(self.missing) if self.missing else "required configuration"
        super().__init__(f"'{provider_id}' is not configured. Missing: {detail}")


class ProviderDisabledError(RuntimeError):
    """An administrator disabled the provider (`ai_providers.enabled = false`). Like a missing
    configuration it is a state, not a failure, and never feeds the circuit breaker."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__(f"'{provider_id}' has been disabled by an administrator")


class ProviderNoModelError(RuntimeError):
    """The registry has no callable model for the provider (all retired/disabled, or none set)."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__(
            f"'{provider_id}' has no usable model in the registry "
            "(every model is retired or disabled, or none is configured)"
        )


@dataclass
class AnalysisContext:
    symbol: str
    indicators: IndicatorBundle
    last_bars: list[dict]  # [{"timestamp": str, "close": float}, ...]
    # Deterministic engine output the provider reasons over (Passage 1 §6).
    regime: dict[str, Any] | None = None
    structure: dict[str, Any] | None = None
    risk: dict[str, Any] | None = None
    # Labelled RAG context (Passage 1 §7) - each chunk carries its own
    # source_type tag so the provider can attribute what it used.
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    timeframe: str = "1d"


@dataclass
class ProviderReasoning:
    direction: Literal["LONG", "SHORT"] | None
    confidence: float | None
    reasoning_summary: str
    supporting_evidence: list[str]
    contradicting_evidence: list[str] = field(default_factory=list)
    # Populated when the provider used registered tools (Passage 1 §4.8).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HealthResult:
    """Return shape for POST /api/ai/{provider}/test and GET .../health."""

    ok: bool
    latency_ms: float | None
    model_checked: str | None
    detail: str
    transport: Literal["hosted_api", "local_model", "none"] = "none"


@runtime_checkable
class AIProvider(Protocol):
    id: str
    display_name: str
    supports_tools: bool
    supports_local_runtime: bool

    def is_configured(self) -> bool: ...

    def missing_configuration(self) -> list[str]: ...

    # `model_id` is passed ONLY when the router substitutes the provider's fallback model
    # (providers/registry.py); a normal call is `reason(context)` and the provider picks its
    # own configured model.
    async def reason(
        self, context: AnalysisContext, model_id: str | None = None
    ) -> ProviderReasoning: ...

    async def health_check(self) -> HealthResult: ...