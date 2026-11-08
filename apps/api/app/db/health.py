"""
`ai_health_checks` writer — Passage 1 §9.1: "health-check history (was
runtime-only behavior in Revision 2, now persisted so the Passage-2 admin
monitoring dashboard has something to render)".

Written by every provider probe (`POST /api/ai/{id}/test`, `GET
/api/ai/{id}/health`). `status` is 'ok' or 'down' here; 'degraded' is reserved
for the monitoring phase, which can judge it against a latency baseline this
module does not have.

Best-effort: a probe's result is returned to the admin whether or not it could
be recorded.
"""

from __future__ import annotations

from ..observability import get_logger
from .client import get_pool, is_db_configured
from .signals import get_or_seed_model, get_or_seed_provider

logger = get_logger("db.health")

_NO_MODEL = (None, "", "unset")


async def record_health_check(
    provider_name: str,
    model_id: str | None,
    ok: bool,
    latency_ms: float | None,
    detail: str | None,
) -> bool:
    if not is_db_configured():
        return False
    try:
        provider_db_id = await get_or_seed_provider(provider_name)
        # "unset" (a Custom AI endpoint with no model id configured) is not a
        # model; do not invent an ai_models row for it.
        model_db_id = None if model_id in _NO_MODEL else (await get_or_seed_model(provider_name, str(model_id))).model_db_id
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO ai_health_checks (provider_id, model_id, status, latency_ms, error_detail)
            VALUES ($1, $2, $3, $4, $5)
            """,
            provider_db_id,
            model_db_id,
            "ok" if ok else "down",
            round(latency_ms) if latency_ms is not None else None,
            None if ok else (detail or "")[:1000].replace("\x00", ""),
        )
        return True
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "failed to persist health check", extra={"provider": provider_name, "error": str(err)}
        )
        return False
