"""
`error_logs` writer — Passage 4 §5.2: error_logs(id, error_id, ts, type, message,
stack, route, user_id, provider, severity, status).

The unhandled-error handler (`app/errors.py`) gives the client a short
`errorId` to quote. Before this module that id existed only in the response
body and in a log line; now the same id is the `error_id` column of a durable
row, which is what the Error Center (a later phase) reads and triages
(`status`: open -> ack -> closed; the partial index from migration 0001 covers
the open backlog).

Best-effort: failing to record an error must never turn into a second failure
inside the error handler.
"""

from __future__ import annotations

from ..observability import get_logger
from .client import get_pool, is_db_configured

logger = get_logger("db.errors")

SEVERITIES = ("low", "medium", "high", "critical")

_MAX_MESSAGE = 2_000
_MAX_STACK = 20_000


def _clean(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value.replace("\x00", "")[:limit]


async def record_error(
    *,
    error_id: str,
    error_type: str,
    message: str,
    stack: str | None = None,
    route: str | None = None,
    user_id: str | None = None,
    provider: str | None = None,
    severity: str = "high",
) -> bool:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    if not is_db_configured():
        return False
    try:
        pool = await get_pool()
        # The sub-select turns a validly-signed token for an account that no
        # longer exists into NULL instead of a foreign-key failure that would
        # lose the row.
        await pool.execute(
            """
            INSERT INTO error_logs
              (error_id, type, message, stack, route, user_id, provider, severity)
            VALUES ($1, $2, $3, $4, $5, (SELECT id FROM users WHERE id = $6::uuid), $7, $8)
            """,
            error_id,
            _clean(error_type, 200),
            _clean(message, _MAX_MESSAGE) or "",
            _clean(stack, _MAX_STACK),
            _clean(route, 500),
            user_id,
            provider,
            severity,
        )
        return True
    except Exception as err:  # noqa: BLE001
        logger.warning("failed to persist error log", extra={"errorId": error_id, "error": str(err)})
        return False
