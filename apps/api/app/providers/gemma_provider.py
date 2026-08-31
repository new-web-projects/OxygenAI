"""
Identity vs. transport, per the blueprint's own §4.3.1 rule: Gemma 4 is
the provider identity; Google's Gemini API is only the hosted transport
that reaches it. This file is GemmaProvider — nothing here is named or
exposed as a separate "Gemini" provider. Port of
lib/providers/gemmaProvider.ts, now using the real Python google-genai
SDK the blueprint names explicitly (confirmed on PyPI before writing
this — see the delivery notes).

Verify MODEL_ID against Google's current model list before relying on
it — Gemma 4 model ID strings have shown minor naming variation across
release waves (see README). Override with GEMMA_MODEL_ID if it's changed.
"""

from __future__ import annotations

import json
import os
import re

from google import genai

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt


class GemmaProvider:
    id = "gemma"
    display_name = "Gemma 4 (hosted via Gemini API transport)"

    def is_configured(self) -> bool:
        return bool(os.environ.get("GOOGLE_AI_API_KEY"))

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("GOOGLE_AI_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY is not set")

        model_id = os.environ.get("GEMMA_MODEL_ID", "gemma-4-4b-it")

        # @google/genai (JS) / google-genai (Python) is the SDK Google
        # ships for reaching Gemini AND Gemma — using it is the
        # transport, not a second provider. See module docstring.
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model_id,
            contents=build_prompt(context),
        )

        text = response.text or ""
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Gemma response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


gemma_provider = GemmaProvider()
