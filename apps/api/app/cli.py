"""
Operator commands — the only supported way to create or promote an admin.

Why this exists: registration always yields the `user` role (there is
deliberately no HTTP route that grants a role), and migration 0004 seeds
the `admin` role but no admin account. Before this module the admin-only
endpoints (provider test/health, knowledge upload/delete) were
unreachable on a fresh install short of hand-written SQL.

    python -m app.cli create-admin --email you@example.com
    python -m app.cli set-role --email you@example.com --role admin
    python -m app.cli purge-expired

The password is read from an interactive prompt (or, for scripted use,
one line on stdin with --password-stdin). It is never accepted as a
command-line argument — arguments land in shell history and `ps`.
Requires DATABASE_URL (read from the same .env files the API uses).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from .config import load_env_files

MIN_PASSWORD_LENGTH = 12  # mirrors schemas.RegisterRequest


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password (min 12 characters): ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def _create_admin(email: str, password: str) -> str:
    from .db.audit import record_audit
    from .db.client import close_pool
    from .db.users import create_user, get_user_by_email, set_user_role
    from .security.passwords import hash_password

    email = email.strip().lower()
    try:
        existing = await get_user_by_email(email)
        if existing is not None:
            await set_user_role(email, "admin")
            await record_audit(
                actor_id=None,
                action="user.role_set",
                resource=f"user:{existing.id}",
                before={"role": existing.role_name},
                after={"role": "admin", "via": "cli"},
            )
            return f"User {email} already existed — role set to admin (password unchanged)."
        created = await create_user(email, hash_password(password), role_name="admin")
        await record_audit(
            actor_id=None,
            action="user.create_admin",
            resource=f"user:{created.id}",
            after={"role": "admin", "via": "cli"},
        )
        return f"Admin account created for {email}."
    finally:
        await close_pool()


async def _set_role(email: str, role: str) -> str:
    from .db.audit import record_audit
    from .db.client import close_pool
    from .db.users import get_user_by_email, set_user_role

    email = email.strip().lower()
    try:
        before = await get_user_by_email(email)
        ok = await set_user_role(email, role)
        if ok and before is not None:
            await record_audit(
                actor_id=None,
                action="user.role_set",
                resource=f"user:{before.id}",
                before={"role": before.role_name},
                after={"role": role, "via": "cli"},
            )
    finally:
        await close_pool()
    return f"{email} is now '{role}'." if ok else f"No such user '{email}' or role '{role}' — nothing changed."


async def _purge_expired() -> str:
    from .db.client import close_pool
    from .db.retention import run_retention_once

    try:
        report = await run_retention_once()
    finally:
        await close_pool()
    suffix = f" ({report.errors} purge step(s) failed — see the log)" if report.errors else ""
    return (
        f"Deleted {report.sessions_deleted} expired session(s) and "
        f"{report.memories_deleted} expired memory row(s).{suffix}"
    )


def main(argv: list[str] | None = None) -> int:
    load_env_files()
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="Create an admin account, or promote an existing user")
    create.add_argument("--email", required=True)
    create.add_argument("--password-stdin", action="store_true", help="Read the password from one line of stdin")

    role = sub.add_parser("set-role", help="Set an existing user's role")
    role.add_argument("--email", required=True)
    role.add_argument("--role", required=True, choices=["admin", "user"])

    sub.add_parser("purge-expired", help="Delete expired sessions and expired short-lived memories")

    args = parser.parse_args(argv)

    from .db.client import is_db_configured

    if not is_db_configured():
        print("DATABASE_URL is not set — nothing to do.", file=sys.stderr)
        return 2

    if args.command == "create-admin":
        message = asyncio.run(_create_admin(args.email, _read_password(args.password_stdin)))
    elif args.command == "purge-expired":
        message = asyncio.run(_purge_expired())
    else:
        message = asyncio.run(_set_role(args.email, args.role))
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
