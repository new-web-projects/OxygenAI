"""
Port of lib/db/client.ts. Lazily-created connection pool, gated on
DATABASE_URL being set.
"""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


def is_db_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def get_pool() -> asyncpg.Pool:
    global _pool
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
