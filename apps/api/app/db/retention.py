"""
Retention — Passage 4 §5.3 / PLAN.md G12: sessions and short-lived memories
expire; `audit_logs` and `trade_results` are kept indefinitely (nothing in this
module touches them); there is no tick-level `market_data` to roll up.

`run_retention_once()` is the unit of work. It runs three ways, all calling the
same function: periodically inside the API process (`RETENTION_INTERVAL_SECONDS`,
see `main.py`), on demand with `python -m app.cli purge-expired`, and from
tests. Deletes are idempotent, so several API instances running the loop at
once is harmless — it only costs redundant no-op statements.

`sessions` already could not authenticate once expired (`session_is_valid`
checks `expires_at > now()`); this reclaims the rows, which are otherwise one
per login forever.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..observability import get_logger
from .client import get_pool, is_db_configured

logger = get_logger("db.retention")


@dataclass(frozen=True)
class RetentionReport:
    sessions_deleted: int
    memories_deleted: int
    errors: int = 0

    @property
    def total(self) -> int:
        return self.sessions_deleted + self.memories_deleted


def _deleted_count(command_tag: str) -> int:
    # asyncpg returns the command tag, e.g. "DELETE 3".
    try:
        return int(command_tag.split()[-1])
    except (ValueError, IndexError):
        return 0


async def purge_expired_sessions() -> int:
    pool = await get_pool()
    return _deleted_count(await pool.execute("DELETE FROM sessions WHERE expires_at <= now()"))


async def purge_expired_memories() -> int:
    """Only rows that HAVE an expiry: long-term memory (`expires_at IS NULL`) lives until the user deletes it."""
    pool = await get_pool()
    return _deleted_count(
        await pool.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= now()")
    )


async def run_retention_once() -> RetentionReport:
    """One pass. A failure in one purge is logged and counted; it never blocks the other."""
    if not is_db_configured():
        return RetentionReport(0, 0)
    sessions = memories = errors = 0
    try:
        sessions = await purge_expired_sessions()
    except Exception as err:  # noqa: BLE001
        errors += 1
        logger.warning("session purge failed", extra={"error": str(err)})
    try:
        memories = await purge_expired_memories()
    except Exception as err:  # noqa: BLE001
        errors += 1
        logger.warning("memory purge failed", extra={"error": str(err)})
    report = RetentionReport(sessions, memories, errors)
    if report.total:
        logger.info(
            "retention purge",
            extra={"sessionsDeleted": sessions, "memoriesDeleted": memories},
        )
    return report


async def retention_loop(interval_seconds: float) -> None:
    """Runs `run_retention_once` immediately, then every `interval_seconds`, until cancelled."""
    while True:
        try:
            await run_retention_once()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must outlive any single failure
            logger.warning("retention pass crashed", extra={"error": str(err)})
        await asyncio.sleep(interval_seconds)
