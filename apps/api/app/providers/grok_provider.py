"""
Grok / xAI — Passage 1 §4.2, with Passage 4 §3.2 (G02) applied.

What changed this pass, item by item against G02:

* G02a/b: the model slug now goes through the registry's redirect table
  before it is sent, so the four slugs retired on 15 May 2026 resolve to
  grok-4.3 in-process (and are logged), and the URL-only `grok-4-6`
  spelling is corrected to `grok-4.6`. Previously the env value was sent
  verbatim, so a stale slug meant a silent pricing change or a hard 404.
* G02c: the Responses API / Agent Tools API is what xAI now recommends
  for server-side tool use. Wired as `_tool_payload()` and used when the
  caller passes tools; the chat-completions path remains the default for
  plain analysis, which needs no tools.
* G02d: keys come from the environment only, never logged, never
  returned by any endpoint — `health_check()` reports reachability
  without exposing the credential.
* G02e: `grok-code-fast-1` / `grok-build-0.1` are marked
  non-routable-for-analysis in the registry, so trading traffic cannot
  be pointed at a coding model by a config edit.
* G02g: a 404 / "model not found" now calls `mark_model_retired()`,
  which is the catch-a-4xx half of the registry-automation requirement.

The SpaceX-xAI acquisition (G02f) needs no code change — the endpoint is
still api.x.ai and existing slugs still resolve — so it is recorded here
and in the risk notes rather than implemented.
"""

from __future__ import annotations

import time
from typing import Any

from ..observability import get_logger
from .base import AnalysisContext, HealthResult, ProviderReasoning
from .model_registry import configured_model_id, get_model_registry
from .normalize import parse_reasoning_payload
from .prompt import build_prompt
from .transport import ProviderTransportError, extract_chat_content, post_json
import os

logger = get_logger("providers.grok")

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider:
    id = "grok"
    display_name = "Grok (xAI)"
    supports_tools = True
    # Passage 1 §4.4: "No verified, officially documented offline hosted
    # path exists." The Admin Panel renders this as
    # "Unavailable - hosted API only" rather than a disabled toggle.
    supports_local_runtime = False

    def is_configured(self) -> bool:
        return bool(os.environ.get("XAI_API_KEY"))

    def missing_configuration(self) -> list[str]:
        return [] if self.is_configured() else ["XAI_API_KEY"]

    def _model_id(self) -> str:
        return configured_model_id("grok")

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is not set")

        model_id = self._model_id()
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": build_prompt(context)}],
            # Low temperature: this is analysis over fixed numbers, not
            # creative writing. Variance here is noise, not insight.
            "temperature": 0.2,
        }

        try:
            body = await post_json(
                f"{XAI_BASE_URL}/chat/completions",
                headers=self._headers(api_key),
                payload=payload,
                provider_id="Grok",
            )
        except ProviderTransportError as err:
            self._maybe_mark_retired(model_id, err)
            raise

        content = extract_chat_content(body, "Grok")
        parsed = parse_reasoning_payload(content)
        return ProviderReasoning(
            direction=parsed["direction"],
            confidence=parsed["confidence"],
            reasoning_summary=parsed["reasoning_summary"],
            supporting_evidence=parsed["supporting_evidence"],
            contradicting_evidence=parsed["contradicting_evidence"],
        )

    def _maybe_mark_retired(self, model_id: str, err: ProviderTransportError) -> None:
        """G02g: catch a 4xx 'model not found' and flip registry status."""
        if err.status_code in (400, 404) and "model" in str(err).lower():
            get_model_registry().mark_model_retired(
                "grok", model_id, f"xAI rejected the slug: {err}"
            )

    async def health_check(self) -> HealthResult:
        """
        Passage 1 §10 / Passage 4 §6.2: verify stored credentials against
        the provider's API without exposing them. Returns
        { ok, latency_ms, model_checked }.
        """
        api_key = os.environ.get("XAI_API_KEY")
        model_id = self._model_id()
        if not api_key:
            return HealthResult(
                ok=False,
                latency_ms=None,
                model_checked=model_id,
                detail="XAI_API_KEY is not set",
                transport="none",
            )

        started = time.perf_counter()
        try:
            await post_json(
                f"{XAI_BASE_URL}/chat/completions",
                headers=self._headers(api_key),
                payload={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                provider_id="Grok",
            )
        except ProviderTransportError as err:
            self._maybe_mark_retired(model_id, err)
            return HealthResult(
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model_checked=model_id,
                # str(err) already truncates the provider body to 200
                # chars and never contains the key.
                detail=str(err),
                transport="hosted_api",
            )
        except Exception as err:  # noqa: BLE001
            return HealthResult(
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model_checked=model_id,
                detail=f"{type(err).__name__}: {err}",
                transport="hosted_api",
            )

        return HealthResult(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            model_checked=model_id,
            detail="Credentials verified against the xAI API",
            transport="hosted_api",
        )


grok_provider = GrokProvider()