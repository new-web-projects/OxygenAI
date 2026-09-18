"""
Custom AI ("Oxygen AI" in the UI) - Passage 1 §4.1.

Passage 1 is precise that this is "an orchestrating agent, not a single
model call": base model any strong tool-calling LLM behind the
AIProvider interface, grounding via already-computed features, never
self-computed numbers.

Before this pass only the single-model-call half existed, and the file
said so. The orchestrating-agent half is implemented this pass in
`app/agent/orchestrator.py`, which drives the 15-tool registry Passage 4
§3.5 (G05) recovers. This file remains the provider/transport layer that
the orchestrator calls - the separation matters, because it is what lets
the same agent loop sit over any configured endpoint.

Important distinction the work-start prompt calls out explicitly: being
"your own AI" does not automatically mean an external API key. Three
transports are supported and auto-detected:

  * a local OpenAI-compatible runtime (Ollama, llama.cpp, vLLM,
    LiteRT-LM) - no key required, tagged local_model;
  * a hosted OpenAI-compatible endpoint - key required, tagged
    hosted_api;
  * unconfigured - reported honestly as unavailable, never faked.

Passage 1 §4.4 notes Gemma 4 is "a natural candidate to also serve as
Custom AI's reference local model", which the local transport makes
possible without a second weight set.
"""

from __future__ import annotations

import os
import time

from ..observability import get_logger
from .base import AnalysisContext, HealthResult, ProviderReasoning
from .normalize import parse_reasoning_payload
from .prompt import build_prompt
from .transport import TransportPolicy, extract_chat_content, post_json

logger = get_logger("providers.custom")


class CustomAIProvider:
    id = "custom"
    display_name = "Oxygen AI"
    supports_tools = True
    # Passage 1 §4.4: "Local path is legitimate and fully supported - a
    # self-hosted open-weight model of the owner's choice."
    supports_local_runtime = True

    # ---- configuration -------------------------------------------------

    def base_url(self) -> str:
        return os.environ.get("CUSTOM_AI_BASE_URL", "").strip().rstrip("/")

    def model_id(self) -> str:
        return os.environ.get("CUSTOM_AI_MODEL_ID", "").strip()

    def api_key(self) -> str:
        return os.environ.get("CUSTOM_AI_API_KEY", "").strip()

    def is_local(self) -> bool:
        """
        A local runtime is one explicitly tagged as such, or one whose
        base URL points at the loopback interface. Detecting loopback
        means the common case (Ollama on localhost) needs no extra
        config, while the explicit tag still wins when someone runs a
        local model on a LAN address.
        """
        tag = os.environ.get("CUSTOM_AI_SOURCE_TAG", "").strip().lower()
        if tag == "local_model":
            return True
        if tag == "hosted_api":
            return False
        url = self.base_url()
        return any(host in url for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

    def is_configured(self) -> bool:
        # A local runtime legitimately has no API key. Requiring one was
        # the previous behaviour and it made the supported local path
        # impossible to configure.
        if not self.base_url() or not self.model_id():
            return False
        return True if self.is_local() else bool(self.api_key())

    def missing_configuration(self) -> list[str]:
        missing: list[str] = []
        if not self.base_url():
            missing.append("CUSTOM_AI_BASE_URL")
        if not self.model_id():
            missing.append("CUSTOM_AI_MODEL_ID")
        if not self.is_local() and not self.api_key():
            missing.append("CUSTOM_AI_API_KEY (not required for a local runtime)")
        return missing

    def source_tag(self) -> str:
        return "local_model" if self.is_local() else "hosted_api"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ---- inference -----------------------------------------------------

    async def complete(self, prompt: str, *, temperature: float = 0.2) -> str:
        """
        Raw completion. Public because the agent orchestrator
        (app/agent/orchestrator.py) drives multi-turn tool-calling
        through it rather than through `reason()`.
        """
        if not self.is_configured():
            raise RuntimeError(
                "Custom AI is not configured. Required: "
                + ", ".join(self.missing_configuration())
            )
        body = await post_json(
            f"{self.base_url()}/chat/completions",
            headers=self._headers(),
            payload={
                "model": self.model_id(),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            },
            provider_id="Custom AI",
        )
        return extract_chat_content(body, "Custom AI")

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        content = await self.complete(build_prompt(context))
        parsed = parse_reasoning_payload(content)
        return ProviderReasoning(
            direction=parsed["direction"],
            confidence=parsed["confidence"],
            reasoning_summary=parsed["reasoning_summary"],
            supporting_evidence=parsed["supporting_evidence"],
            contradicting_evidence=parsed["contradicting_evidence"],
        )

    async def health_check(self) -> HealthResult:
        model_id = self.model_id() or "unset"
        if not self.is_configured():
            return HealthResult(
                ok=False,
                latency_ms=None,
                model_checked=model_id,
                detail="Not configured. Missing: " + ", ".join(self.missing_configuration()),
                transport="none",
            )
        started = time.perf_counter()
        try:
            await post_json(
                f"{self.base_url()}/chat/completions",
                headers=self._headers(),
                payload={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                provider_id="Custom AI",
                policy=TransportPolicy(timeout_seconds=8.0, max_attempts=1),
            )
        except Exception as err:  # noqa: BLE001
            return HealthResult(
                ok=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model_checked=model_id,
                detail=f"{type(err).__name__}: {str(err)[:200]}",
                transport=self.source_tag(),  # type: ignore[arg-type]
            )
        return HealthResult(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            model_checked=model_id,
            detail=f"Endpoint reachable ({self.source_tag()})",
            transport=self.source_tag(),  # type: ignore[arg-type]
        )


custom_ai_provider = CustomAIProvider()