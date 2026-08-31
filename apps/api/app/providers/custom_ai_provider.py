"""
"Custom AI" per the blueprint is deliberately not a fixed vendor — §4.1
defines it as an orchestrating agent over "any strong tool-calling LLM
behind the AIProvider interface," the owner's choice. This is that choice
made concrete: any OpenAI-compatible chat-completions endpoint, configured
via env vars. Displayed in the UI as "Oxygen AI" (the product's own name
for its default provider) — the id stays "custom" throughout the backend.

NOT implemented here: the "orchestrating agent" half of the spec — tool
calling against the 15-tool registry, the RAG -> feedback loop ->
fine-tuning evolution path (blueprint §4.1, §4.8). This is the provider
integration layer only. Port of lib/providers/customAiProvider.ts.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt


class CustomAIProvider:
    id = "custom"
    display_name = "Oxygen AI (your configured model)"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("CUSTOM_AI_API_KEY")
            and os.environ.get("CUSTOM_AI_BASE_URL")
            and os.environ.get("CUSTOM_AI_MODEL_ID")
        )

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("CUSTOM_AI_API_KEY")
        base_url = os.environ.get("CUSTOM_AI_BASE_URL")
        model_id = os.environ.get("CUSTOM_AI_MODEL_ID")
        if not api_key or not base_url or not model_id:
            raise RuntimeError(
                "CUSTOM_AI_API_KEY, CUSTOM_AI_BASE_URL, and CUSTOM_AI_MODEL_ID must all be set"
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": build_prompt(context)}],
                },
            )

        if res.status_code >= 400:
            raise RuntimeError(f"Custom AI endpoint error {res.status_code}: {res.text[:200]}")

        data = res.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Custom AI response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


custom_ai_provider = CustomAIProvider()
