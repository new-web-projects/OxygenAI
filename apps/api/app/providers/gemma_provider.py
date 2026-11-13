"""
Gemma 4 - Passage 1 §4.3, and §4.3.1's identity-vs-transport rule.

The rule, stated by Passage 1 and reinforced by the Correction Prompt:
Gemma 4 is the PROVIDER IDENTITY; Google's Gemini API is only the
HOSTED TRANSPORT that reaches it. Nothing in this file is named, typed,
exposed, or registered as a "Gemini" provider. The class is
GemmaProvider, the id is "gemma", and the credential is deliberately
GOOGLE_AI_API_KEY rather than GEMINI_API_KEY.

New this pass:

* A local-runtime transport (§4.4). Gemma 4 is genuinely open-weight
  (Apache 2.0) and Passage 1 calls its offline story "the strongest of
  the three". This provider now supports an OpenAI-compatible local
  server - Ollama, llama.cpp's server, LiteRT-LM's `serve` mode, or
  vLLM, all of which expose that shape - selected by GEMMA_TRANSPORT.
* The source tag follows the transport, enforcing §4.4's closing rule:
  "no response from a local Gemma runtime may ever be labeled
  hosted_api - it is always local_model."
* Reachability detection, so an unreachable local runtime produces a
  controlled error rather than implying offline inference works.
"""

from __future__ import annotations

import os
import time
from typing import Literal

from ..observability import get_logger
from .base import AnalysisContext, HealthResult, ProviderReasoning
from .lifecycle import is_model_not_found, retire_model
from .model_registry import configured_model_id
from .normalize import parse_reasoning_payload
from .prompt import build_prompt
from .provider_settings import effective_api_key, effective_base_url, effective_policy
from .transport import (
    ProviderTransportError,
    TransportPolicy,
    extract_chat_content,
    post_json,
)

logger = get_logger("providers.gemma")

Transport = Literal["hosted_api", "local_model"]


class GemmaProvider:
    id = "gemma"
    display_name = "Gemma 4"
    # Passage 1 §4.3: Gemma 4 supports native function calling, so it is
    # a fully capable tool-calling provider with no gap to fill.
    supports_tools = True
    # Passage 1 §4.4: first-party open weights, multiple officially
    # documented local runtimes.
    supports_local_runtime = True

    def transport(self) -> Transport:
        configured = os.environ.get("GEMMA_TRANSPORT", "").strip().lower()
        if configured in ("local", "local_model", "offline"):
            return "local_model"
        return "hosted_api"

    def local_base_url(self) -> str:
        # Ollama's default. llama.cpp server, LiteRT-LM `serve` and vLLM
        # all expose the same OpenAI-compatible surface on their own port.
        # An admin's `base_url` override (ai_provider_settings) applies to the local runtime:
        # the hosted transport has no URL to override (Google's SDK owns it).
        return effective_base_url("gemma", "GEMMA_LOCAL_BASE_URL", "http://localhost:11434/v1")

    def _api_key(self) -> str:
        # Admin override (ai_provider_settings) > GOOGLE_AI_API_KEY.
        return effective_api_key("gemma", "GOOGLE_AI_API_KEY")

    def is_configured(self) -> bool:
        if self.transport() == "local_model":
            # A local runtime needs no API key - that is the point of it.
            return bool(self.local_base_url())
        return bool(self._api_key())

    def missing_configuration(self) -> list[str]:
        if self.transport() == "local_model":
            return [] if self.local_base_url() else ["GEMMA_LOCAL_BASE_URL"]
        return [] if self._api_key() else ["GOOGLE_AI_API_KEY"]

    def _model_id(self) -> str:
        return configured_model_id("gemma")

    async def _reason_local(self, context: AnalysisContext, model_id: str) -> str:
        """OpenAI-compatible local server (Ollama / llama.cpp / LiteRT-LM / vLLM)."""
        body = await post_json(
            f"{self.local_base_url()}/chat/completions",
            headers={"Content-Type": "application/json"},
            payload={
                "model": model_id,
                "messages": [{"role": "user", "content": build_prompt(context)}],
                "temperature": 0.2,
            },
            provider_id="Gemma 4 (local runtime)",
            policy=effective_policy("gemma"),
        )
        return extract_chat_content(body, "Gemma 4 (local runtime)")

    async def _reason_hosted(self, context: AnalysisContext, model_id: str) -> str:
        """
        Hosted transport via Google's google-genai SDK.

        The SDK is imported inside the call rather than at module scope
        so that a missing/broken google-genai install degrades this one
        provider instead of breaking the whole API's import graph - the
        same fail-safe principle the native layer uses.
        """
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY is not set")
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as err:  # pragma: no cover - dependency is pinned
            raise RuntimeError(
                "google-genai is not installed; cannot reach the hosted Gemma transport"
            ) from err

        client = genai.Client(api_key=api_key)
        try:
            response = await client.aio.models.generate_content(
                model=model_id, contents=build_prompt(context)
            )
        except Exception as err:  # noqa: BLE001
            # G02g applies "identically to Gemma's model list" (Passage 4 §3.2). The SDK raises
            # its own exception types, so classify on the status code it carries (when it has
            # one) and the text, which must name this very model.
            status = getattr(err, "code", None) or getattr(err, "status_code", None)
            if is_model_not_found(status if isinstance(status, int) else None, str(err), model_id):
                await retire_model("gemma", model_id, f"Google rejected the model: {str(err)[:200]}", detected_by="runtime")
            raise
        text = response.text or ""
        if not text.strip():
            raise RuntimeError("Gemma 4 returned an empty response")
        return text

    async def reason(
        self, context: AnalysisContext, model_id: str | None = None
    ) -> ProviderReasoning:
        model_id = model_id or self._model_id()
        transport = self.transport()

        if transport == "local_model":
            content = await self._reason_local(context, model_id)
        else:
            content = await self._reason_hosted(context, model_id)

        parsed = parse_reasoning_payload(content)
        return ProviderReasoning(
            direction=parsed["direction"],
            confidence=parsed["confidence"],
            reasoning_summary=parsed["reasoning_summary"],
            supporting_evidence=parsed["supporting_evidence"],
            contradicting_evidence=parsed["contradicting_evidence"],
        )

    async def health_check(self) -> HealthResult:
        """
        Passage 1 §10's GET /api/ai/gemma/health: "Reports both hosted
        and local-runtime health where a local runtime is configured."
        """
        model_id = self._model_id()
        transport = self.transport()
        started = time.perf_counter()

        if transport == "local_model":
            try:
                await post_json(
                    f"{self.local_base_url()}/chat/completions",
                    headers={"Content-Type": "application/json"},
                    payload={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                    provider_id="Gemma 4 (local runtime)",
                    policy=TransportPolicy(timeout_seconds=5.0, max_attempts=1),
                )
            except Exception as err:  # noqa: BLE001
                return HealthResult(
                    ok=False,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    model_checked=model_id,
                    detail=(
                        f"Local Gemma runtime at {self.local_base_url()} is not reachable: "
                        f"{type(err).__name__}: {err}"
                    ),
                    transport="local_model",
                )
            return HealthResult(
                ok=True,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model_checked=model_id,
                detail=f"Local Gemma runtime reachable at {self.local_base_url()}",
                transport="local_model",
            )

        if not self._api_key():
            return HealthResult(
                ok=False,
                latency_ms=None,
                model_checked=model_id,
                detail="GOOGLE_AI_API_KEY is not set",
                transport="none",
            )

        try:
            from google import genai  # type: ignore[import-not-found]

            client = genai.Client(api_key=self._api_key())
            await client.aio.models.generate_content(model=model_id, contents="ping")
        except Exception as err:  # noqa: BLE001
            return HealthResult(
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model_checked=model_id,
                detail=f"{type(err).__name__}: {str(err)[:200]}",
                transport="hosted_api",
            )

        return HealthResult(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            model_checked=model_id,
            detail="Credentials verified against the hosted Gemma transport",
            transport="hosted_api",
        )


    async def list_available_models(self) -> set[str] | None:
        """
        Model ids the hosted transport can serve for this key — the periodic half of G02g for
        Gemma. None = "could not tell" (local transport, no key, SDK shape differs, network).
        Not live-verified: it needs a real Google AI key (see docs/implementation/PLAN.md, P3).
        """
        if self.transport() != "hosted_api" or not self._api_key():
            return None
        try:
            from google import genai  # type: ignore[import-not-found]

            client = genai.Client(api_key=self._api_key())
            names: set[str] = set()
            async for model in await client.aio.models.list():
                name = getattr(model, "name", None)
                if isinstance(name, str) and name:
                    names.add(name.split("/")[-1])
            return names or None
        except Exception:  # noqa: BLE001 — inconclusive, never "nothing available"
            return None


gemma_provider = GemmaProvider()