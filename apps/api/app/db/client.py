"""
Port of lib/db/client.ts. Lazily-created connection pool, gated on
DATABASE_URL being set.

Pool creation is serialised: before P2, two requests arriving together on a
cold process each saw `_pool is None`, each awaited `create_pool`, and the
second assignment orphaned the first pool (its connections leaked until
process exit). Reproduced with a counting stand-in for `asyncpg.create_pool`.
`main.py`'s lifespan now also calls `close_pool()` on shutdown.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

_pool: asyncpg.Pool | None = None

# asyncio.Lock is bound to the loop it is first contended on, and the test
# suite gives every test its own loop, so the lock is recreated per loop.
_pool_lock: asyncio.Lock | None = None
_pool_lock_loop: asyncio.AbstractEventLoop | None = None


def _creation_lock() -> asyncio.Lock:
    global _pool_lock, _pool_lock_loop
    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_lock_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
    return _pool_lock


def is_db_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def get_pool() -> asyncpg.Pool:
    global _pool
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")
    if _pool is not None:
        return _pool
    async with _creation_lock():
        if _pool is None:  # re-check: another task may have created it while we waited
            _pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
