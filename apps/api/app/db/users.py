"""
Users, roles, and sessions — reads and writes the `users`, `roles`,
`permissions`, and `sessions` tables. All four already existed in the
schema before this pass (Passage 4 §5.2/G12 restored them; migration
0004 this pass only seeded `roles`/`permissions` rows, not the tables
themselves). This module is the first application code to actually use
them.

Follows the same style as `db/market_data.py` and `db/signals.py`:
plain asyncpg calls through the shared pool, no ORM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .client import get_pool

DEFAULT_ROLE_NAME = "user"


@dataclass(frozen=True)
class UserRecord:
    id: str
    email: str
    password_hash: str
    role_id: str
    role_name: str
    status: str


async def get_role_id_by_name(name: str) -> str | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id FROM roles WHERE name = $1", name)
    return str(row["id"]) if row else None


async def get_user_by_email(email: str) -> UserRecord | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.password_hash, u.role_id, r.name AS role_name, u.status
        FROM users u
        JOIN roles r ON r.id = u.role_id
        WHERE u.email = $1
        """,
        email,
    )
    if row is None:
        return None
    return UserRecord(
        id=str(row["id"]),
        email=row["email"],
        password_hash=row["password_hash"],
        role_id=str(row["role_id"]),
        role_name=row["role_name"],
        status=row["status"],
    )


async def get_user_by_id(user_id: str) -> UserRecord | None:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.password_hash, u.role_id, r.name AS role_name, u.status
        FROM users u
        JOIN roles r ON r.id = u.role_id
        WHERE u.id = $1
        """,
        user_id,
    )
    if row is None:
        return None
    return UserRecord(
        id=str(row["id"]),
        email=row["email"],
        password_hash=row["password_hash"],
        role_id=str(row["role_id"]),
        role_name=row["role_name"],
        status=row["status"],
    )


class EmailAlreadyRegisteredError(Exception):
    pass


async def create_user(email: str, password_hash: str, role_name: str = DEFAULT_ROLE_NAME) -> UserRecord:
    """
    Raises EmailAlreadyRegisteredError on a duplicate email (translating
    the table's existing UNIQUE constraint into a clear application
    error) rather than letting a raw asyncpg.UniqueViolationError escape
    to the generic 500 handler.
    """
    role_id = await get_role_id_by_name(role_name)
    if role_id is None:
        raise RuntimeError(
            f"Role '{role_name}' does not exist — run migration 0004_seed_roles.sql"
        )
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (email, password_hash, role_id)
            VALUES ($1, $2, $3)
            RETURNING id, email, password_hash, role_id, status
            """,
            email,
            password_hash,
            role_id,
        )
    except Exception as err:  # noqa: BLE001 — narrowed below by name, no asyncpg import needed here
        if type(err).__name__ == "UniqueViolationError":
            raise EmailAlreadyRegisteredError(email) from err
        raise
    assert row is not None
    return UserRecord(
        id=str(row["id"]),
        email=row["email"],
        password_hash=row["password_hash"],
        role_id=str(row["role_id"]),
        role_name=role_name,
        status=row["status"],
    )


async def create_session(
    user_id: str, token_hash: str, expires_at: datetime, ip: str | None, user_agent: str | None
) -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent)
        VALUES ($1, $2, $3, $4::inet, $5)
        RETURNING id
        """,
        user_id,
        token_hash,
        expires_at,
        ip,
        user_agent,
    )
    assert row is not None
    return str(row["id"])


async def session_is_valid(token_hash: str) -> bool:
    """
    True only if a session row exists for this token AND it hasn't
    expired. This is the revocability check `security/jwt.py`'s
    docstring describes: deleting the row (see `delete_session_by_hash`)
    makes a not-yet-expired JWT stop working immediately.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM sessions WHERE token_hash = $1 AND expires_at > now()",
        token_hash,
    )
    return row is not None


async def delete_session_by_hash(token_hash: str) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)
    return result != "DELETE 0"


async def delete_all_sessions_for_user(user_id: str) -> int:
    """Log out everywhere — the mechanism a future admin 'revoke sessions' action would call."""
    pool = await get_pool()
    result = await pool.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
    return int(result.split()[-1]) if result else 0


async def get_permissions_for_role(role_name: str) -> list[tuple[str, str]]:
    """Returns [(resource, action), ...] for the given role — the data
    behind `security/dependencies.py`'s `require_permission`."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT p.resource, p.action
        FROM permissions p
        JOIN roles r ON r.id = p.role_id
        WHERE r.name = $1
        """,
        role_name,
    )
    return [(r["resource"], r["action"]) for r in rows]