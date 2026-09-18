"""
Per-request timeout and retry — Passage 1 §4.5's `timeout=cfg.timeout`
and Passage 4 §3.3's insistence that retry/timeout handling is a
*separate* mechanism from the circuit breaker and from multi-provider
task isolation, with all three "required together, not as substitutes
for one another".

Shared by every HTTP-based provider so timeout and retry policy is
configured once rather than re-hardcoded per provider (the previous
code had `timeout=30.0` literal in each file and no retry at all).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from ..config import get_settings
from ..observability import get_logger

logger = get_logger("providers.transport")

T = TypeVar("T")

# 5xx and 429 are worth another attempt; 4xx (bad key, bad model id,
# malformed request) will fail identically every time, so retrying them
# just burns latency and the provider's rate limit.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ProviderTransportError(RuntimeError):
    """A provider call that failed after exhausting its retry budget."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class TransportPolicy:
    timeout_seconds: float
    max_attempts: int
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0

    @classmethod
    def from_settings(cls) -> "TransportPolicy":
        settings = get_settings()
        return cls(
            timeout_seconds=settings.provider_timeout_seconds,
            # retry_count is *retries*, so attempts is retries + 1.
            max_attempts=max(1, settings.provider_retry_count + 1),
        )


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    provider_id: str,
    policy: TransportPolicy | None = None,
) -> T:
    """
    Run `operation`, retrying only genuinely transient failures.

    Backoff is exponential with jitter — without jitter, three providers
    failing together retry in lockstep and hit the recovering service as
    a synchronised burst.
    """
    active = policy or TransportPolicy.from_settings()
    last_error: Exception | None = None

    for attempt in range(1, active.max_attempts + 1):
        try:
            return await operation()
        except ProviderTransportError as err:
            last_error = err
            if not err.retryable or attempt == active.max_attempts:
                raise
        except (httpx.TimeoutException, httpx.NetworkError) as err:
            last_error = ProviderTransportError(
                f"{type(err).__name__}: {err}", retryable=True
            )
            if attempt == active.max_attempts:
                raise last_error from err
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — unknown failures are not retried
            raise

        delay = min(
            active.max_backoff_seconds, active.base_backoff_seconds * (2 ** (attempt - 1))
        )
        delay += random.uniform(0, delay * 0.25)
        logger.info(
            "retrying provider call",
            extra={
                "provider": provider_id,
                "attempt": attempt,
                "maxAttempts": active.max_attempts,
                "delaySeconds": round(delay, 2),
            },
        )
        await asyncio.sleep(delay)

    raise last_error or ProviderTransportError(f"{provider_id} failed with no recorded error")


async def post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider_id: str,
    policy: TransportPolicy | None = None,
) -> dict[str, Any]:
    """POST JSON with the shared timeout/retry policy applied."""
    active = policy or TransportPolicy.from_settings()

    async def _once() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=active.timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            raise ProviderTransportError(
                f"{provider_id} API error {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        try:
            body = response.json()
        except ValueError as err:
            raise ProviderTransportError(
                f"{provider_id} returned a non-JSON response body"
            ) from err
        if not isinstance(body, dict):
            raise ProviderTransportError(f"{provider_id} returned an unexpected response shape")
        return body

    return await with_retries(_once, provider_id=provider_id, policy=active)


def extract_chat_content(body: dict[str, Any], provider_id: str) -> str:
    """Pull the assistant message out of an OpenAI-compatible response."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderTransportError(f"{provider_id} response contained no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ProviderTransportError(f"{provider_id} response contained no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderTransportError(f"{provider_id} returned an empty message")
    return content