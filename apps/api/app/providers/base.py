"""
Port of lib/providers/types.ts. Every provider (mock, Custom AI, Grok,
Gemma 4) implements this one protocol. Adding a provider is a new file,
not a change to the engine, the schema, or the router — the actual point
of the blueprint's "provider-agnostic AI layer".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..schemas import IndicatorBundle


@dataclass
class AnalysisContext:
    symbol: str
    indicators: IndicatorBundle
    last_bars: list[dict]  # [{"timestamp": str, "close": float}, ...]


@dataclass
class ProviderReasoning:
    direction: Literal["LONG", "SHORT"] | None
    confidence: float | None
    reasoning_summary: str
    supporting_evidence: list[str]


class AIProvider(Protocol):
    id: str
    display_name: str

    def is_configured(self) -> bool: ...

    async def reason(self, context: AnalysisContext) -> ProviderReasoning: ...
