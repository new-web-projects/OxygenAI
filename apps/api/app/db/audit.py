"""
`audit_logs` writer — Passage 4 §5.2: audit_logs(id, actor_id, action, resource,
before jsonb, after jsonb, ts), kept indefinitely and never silently lost
(`actor_id` is ON DELETE RESTRICT, §5.3).

First application code to write this table (G12 ownership: the Ops subsystem).
Admin-visible mutations call `record_audit`; the read side (admin audit-log
viewer) is a later phase.

Best-effort by design: an audit-write failure is logged loudly but never fails
or reverts the action it describes — the same rule `db/tools.log_tool_call`
follows. Returns whether the row was written so callers and tests can tell.
"""

from __future__ import annotations

import json
from typing import Any

from ..observability import get_logger
from .client import get_pool, is_db_configured

logger = get_logger("db.audit")


def _clean(value: str) -> str:
    # Postgres text cannot hold NUL.
    return value.replace("\x00", "")


async def record_audit(
    *,
    actor_id: str | None,
    action: str,
    resource: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> bool:
    if not is_db_configured():
        return False
    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO audit_logs (actor_id, action, resource, before, after)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
            """,
            actor_id,
            _clean(action)[:200],
            _clean(resource)[:500],
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
        )
        return True
    except Exception as err:  # noqa: BLE001 — auditing must never break the audited action
        logger.error(
            "failed to write audit log",
            extra={"action": action, "resource": resource, "error": str(err)},
        )
        return False
