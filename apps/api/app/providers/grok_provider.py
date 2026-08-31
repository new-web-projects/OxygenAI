"""
xAI's API is OpenAI-compatible (chat completions shape), so this uses a
plain httpx call rather than pulling in an SDK for one endpoint — same
"don't add a dependency you don't need" principle the blueprint states
for plugin/library choices. Port of lib/providers/grokProvider.ts.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider:
    id = "grok"
    display_name = "Grok (xAI)"

    def is_configured(self) -> bool:
        return bool(os.environ.get("XAI_API_KEY"))

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is not set")

        model_id = os.environ.get("GROK_MODEL_ID", "grok-4.6")

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{XAI_BASE_URL}/chat/completions",
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
            raise RuntimeError(f"xAI API error {res.status_code}: {res.text[:200]}")

        data = res.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Grok response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


grok_provider = GrokProvider()
