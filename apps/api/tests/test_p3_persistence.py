"""
Phase P3 — the database-backed half of the model registry and provider administration, against a
real Postgres (skipped without DATABASE_URL). Rows created here use the provider `custom` and ids
prefixed `zzp3-`; every admin-managed table is restored by the `p3_admin_state` fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — P3 persistence tests require a live Postgres instance",
)

MIGRATION_0007 = Path(__file__).resolve().parents[3] / "infra" / "migrations" / "0007_provider_registry.sql"


def _id() -> str:
    return f"zzp3-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Migration 0007
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraints_enforce_the_registry_invariants(p3_pool):
    from app.db.signals import get_or_seed_provider

    provider = await get_or_seed_provider("custom")
    async with p3_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            async def attempt(sql: str, *args, expect):
                with pytest.raises(expect):
                    async with conn.transaction():
                        await conn.execute(sql, *args)

            await conn.execute("UPDATE ai_models SET is_default = false, is_fallback = false WHERE provider_id = $1", provider)
            ins = "INSERT INTO ai_models (provider_id, model_id, display_name, status, is_default, is_fallback) VALUES ($1, $2, 'x', $3, $4, $5)"
            await conn.execute(ins, provider, "zzp3-d1", "active", True, False)
            await attempt(ins, provider, "zzp3-d2", "active", True, False, expect=asyncpg.UniqueViolationError)
            await attempt(ins, provider, "zzp3-both", "active", False, True, expect=asyncpg.CheckViolationError) if False else None
            await attempt(ins, provider, "zzp3-b", "active", True, True, expect=(asyncpg.CheckViolationError, asyncpg.UniqueViolationError))
            await attempt(ins, provider, "zzp3-rd", "retired", True, False, expect=asyncpg.CheckViolationError)
            await attempt(ins, provider, "zzp3-fd", "fallback", True, False, expect=asyncpg.CheckViolationError)
            await conn.execute(ins, provider, "zzp3-f1", "fallback", False, True)
            await attempt(ins, provider, "zzp3-f2", "active", False, True, expect=asyncpg.UniqueViolationError)
            await attempt(ins, provider, "zzp3-fdep", "deprecated", False, True, expect=asyncpg.CheckViolationError)
            await attempt(ins, provider, "zzp3-fdis", "disabled", False, True, expect=asyncpg.CheckViolationError)
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_provider_settings_are_one_row_per_provider_with_optional_overrides(p3_pool):
    from app.db.signals import get_or_seed_provider

    provider = await get_or_seed_provider("custom")
    async with p3_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("DELETE FROM ai_provider_settings WHERE provider_id = $1", provider)
            await conn.execute("INSERT INTO ai_provider_settings (provider_id) VALUES ($1)", provider)
            row = await conn.fetchrow("SELECT timeout_ms, retry_count FROM ai_provider_settings WHERE provider_id = $1", provider)
            assert row["timeout_ms"] is None and row["retry_count"] is None  # a row is NOT an override
            for sql in (
                "INSERT INTO ai_provider_settings (provider_id) VALUES ($1)",
                "UPDATE ai_provider_settings SET timeout_ms = 50 WHERE provider_id = $1",
                "UPDATE ai_provider_settings SET timeout_ms = 300001 WHERE provider_id = $1",
                "UPDATE ai_provider_settings SET retry_count = 11 WHERE provider_id = $1",
                "UPDATE ai_provider_settings SET retry_count = -1 WHERE provider_id = $1",
            ):
                with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)):
                    async with conn.transaction():
                        await conn.execute(sql, provider)
            await conn.execute("UPDATE ai_provider_settings SET timeout_ms = 100, retry_count = 10 WHERE provider_id = $1", provider)
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_migration_0007_repairs_violating_rows_and_is_idempotent(p3_pool):
    from app.db.signals import get_or_seed_provider

    sql = MIGRATION_0007.read_text()
    provider = await get_or_seed_provider("custom")
    async with p3_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            for index in ("uq_ai_models_one_default_per_provider", "uq_ai_models_one_fallback_per_provider", "uq_ai_provider_settings_provider"):
                await conn.execute(f"DROP INDEX IF EXISTS {index}")
            for name in ("ai_models_default_not_fallback", "ai_models_default_is_callable", "ai_models_fallback_is_callable"):
                await conn.execute(f"ALTER TABLE ai_models DROP CONSTRAINT IF EXISTS {name}")
            await conn.execute("DELETE FROM ai_provider_settings WHERE provider_id = $1", provider)
            await conn.execute("UPDATE ai_models SET is_default = false, is_fallback = false WHERE provider_id = $1", provider)

            plant = ("INSERT INTO ai_models (provider_id, model_id, display_name, status, is_default, is_fallback, updated_at) "
                     "VALUES ($1, $2, 'x', $3, $4, $5, $6)")
            await conn.execute(plant, provider, "zzp3-old-default", "active", True, False, datetime(2026, 1, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-new-default", "active", True, False, datetime(2026, 2, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-retired-default", "retired", True, False, datetime(2026, 3, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-fb-old", "fallback", False, True, datetime(2026, 1, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-fb-new", "fallback", False, True, datetime(2026, 2, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-deprecated-fb", "deprecated", False, True, datetime(2026, 3, 1, tzinfo=timezone.utc))
            await conn.execute(plant, provider, "zzp3-both", "active", True, True, datetime(2026, 4, 1, tzinfo=timezone.utc))
            ins = "INSERT INTO ai_provider_settings (provider_id, base_url, updated_at) VALUES ($1, $2, $3)"
            await conn.execute(ins, provider, "http://old", datetime(2026, 1, 1, tzinfo=timezone.utc))
            await conn.execute(ins, provider, "http://new", datetime(2026, 2, 1, tzinfo=timezone.utc))

            await conn.execute(sql)
            flags = {r["model_id"]: (r["is_default"], r["is_fallback"]) for r in await conn.fetch(
                "SELECT model_id, is_default, is_fallback FROM ai_models WHERE provider_id = $1 AND model_id LIKE 'zzp3-%'", provider)}
            assert flags["zzp3-retired-default"] == (False, False)      # a retired model cannot stay default
            assert flags["zzp3-deprecated-fb"] == (False, False)        # nor a deprecated one the fallback
            assert flags["zzp3-both"] == (True, False)                  # default wins over fallback...
            # ...then "one default per provider": the newest remaining default (zzp3-both, April) survives.
            assert [m for m, (d, _) in flags.items() if d] == ["zzp3-both"]
            assert [m for m, (_, f) in flags.items() if f] == ["zzp3-fb-new"]
            assert await conn.fetchval("SELECT base_url FROM ai_provider_settings WHERE provider_id = $1", provider) == "http://new"
            assert await conn.fetchval("SELECT count(*) FROM ai_provider_settings WHERE provider_id = $1", provider) == 1

            await conn.execute(sql)  # idempotent
            assert await conn.fetchval("SELECT count(*) FROM ai_models WHERE provider_id = $1 AND is_default", provider) == 1
        finally:
            await tx.rollback()
    cols = {r["column_name"]: (r["is_nullable"], r["column_default"]) for r in await p3_pool.fetch(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'ai_provider_settings' AND column_name IN ('timeout_ms', 'retry_count')")}
    assert cols == {"timeout_ms": ("YES", None), "retry_count": ("YES", None)}


# --------------------------------------------------------------------------
# db/models.py
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_reference_list_covers_every_foreign_key_to_ai_models(p3_pool):
    from app.db.models import MODEL_REFERENCES

    rows = await p3_pool.fetch(
        """
        SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
          FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.contype = 'f' AND c.confrelid = 'ai_models'::regclass
        """
    )
    assert {(r["tbl"], r["col"]) for r in rows} == set(MODEL_REFERENCES), (
        "a table now references ai_models; add it to db/models.py MODEL_REFERENCES so deleting a model "
        "with history is still refused"
    )


@pytest.mark.asyncio
async def test_creating_a_default_demotes_the_previous_default_atomically(p3_admin_state):
    from app.db.models import create_model_row, list_model_rows

    first = await create_model_row("custom", _id(), "First", is_default=True)
    second = await create_model_row("custom", _id(), "Second", is_default=True)
    rows = {r.id: r for r in await list_model_rows("custom")}
    assert rows[first.id].is_default is False and rows[second.id].is_default is True
    assert sum(r.is_default for r in rows.values()) == 1


@pytest.mark.asyncio
async def test_retiring_the_default_promotes_the_fallback_and_empties_its_slot(p3_admin_state):
    from app.db.models import create_model_row, list_model_rows, update_model_row

    default = await create_model_row("custom", _id(), "D", is_default=True)
    fallback = await create_model_row("custom", _id(), "F", status="fallback", is_fallback=True)
    before, after = await update_model_row(default.id, status="retired")  # one call: demote, then repair
    assert (after.status, after.is_default, after.is_fallback) == ("retired", False, False)
    rows = {r.id: r for r in await list_model_rows("custom")}
    assert rows[fallback.id].is_default is True and rows[fallback.id].is_fallback is False
    assert rows[fallback.id].status == "active"  # a default can no longer be "callable only as fallback"


@pytest.mark.asyncio
async def test_a_default_with_no_fallback_just_leaves_the_provider_without_one(p3_admin_state):
    from app.db.models import create_model_row, list_model_rows, update_model_row

    await p3_admin_state if False else None
    rows_before = await list_model_rows("custom")
    for r in rows_before:  # start from a provider with exactly one default and nothing else flagged
        await update_model_row(r.id, is_default=False, is_fallback=False)
    only = await create_model_row("custom", _id(), "Only", is_default=True)
    await update_model_row(only.id, status="disabled")
    assert [r for r in await list_model_rows("custom") if r.is_default] == []


@pytest.mark.asyncio
async def test_contradictory_changes_are_rejected_with_precise_errors(p3_admin_state):
    from app.db.models import ModelConflictError, ModelNotFoundError, ModelValidationError, create_model_row, update_model_row

    model_id = _id()
    row = await create_model_row("custom", model_id, "X")
    with pytest.raises(ModelValidationError, match="cannot be the default"):
        await update_model_row(row.id, status="retired", is_default=True)
    with pytest.raises(ModelValidationError, match="both"):
        await update_model_row(row.id, is_default=True, is_fallback=True)
    with pytest.raises(ModelValidationError, match="status must be one of"):
        await update_model_row(row.id, status="experimental")
    with pytest.raises(ModelConflictError, match="already exists"):
        await create_model_row("custom", model_id, "Duplicate")
    with pytest.raises(ModelNotFoundError):
        await update_model_row(str(uuid.uuid4()), display_name="nope")
    with pytest.raises(ValueError, match="unsupported"):
        await update_model_row(row.id, model_id="renamed")


@pytest.mark.asyncio
async def test_update_merges_config_instead_of_replacing_it(p3_admin_state):
    from app.db.models import create_model_row, update_model_row

    row = await create_model_row("custom", _id(), "C", config={"a": 1, "b": 2})
    _, after = await update_model_row(row.id, config={"b": 3, "c": 4})
    assert after.config == {"a": 1, "b": 3, "c": 4}


@pytest.mark.asyncio
async def test_a_model_with_history_cannot_be_deleted_and_one_without_can(p3_admin_state, p3_pool):
    from app.db.models import ModelInUseError, create_model_row, delete_model_row, get_model_row

    row = await create_model_row("custom", _id(), "H")
    await p3_pool.execute("INSERT INTO ai_health_checks (provider_id, model_id, status) VALUES ($1, $2, 'ok')", uuid.UUID(row.provider_db_id), uuid.UUID(row.id))
    with pytest.raises(ModelInUseError) as raised:
        await delete_model_row(row.id)
    assert raised.value.references == {"ai_health_checks": 1} and "retired" in str(raised.value)
    assert await get_model_row(row.id) is not None
    await p3_pool.execute("DELETE FROM ai_health_checks WHERE model_id = $1", uuid.UUID(row.id))
    assert (await delete_model_row(row.id)).id == row.id
    assert await get_model_row(row.id) is None


@pytest.mark.asyncio
async def test_deleting_the_default_promotes_the_fallback(p3_admin_state):
    from app.db.models import create_model_row, delete_model_row, list_model_rows

    default = await create_model_row("custom", _id(), "D", is_default=True)
    fallback = await create_model_row("custom", _id(), "F", status="fallback", is_fallback=True)
    await delete_model_row(default.id)
    promoted = [r for r in await list_model_rows("custom") if r.is_default]
    assert [r.id for r in promoted] == [fallback.id] and promoted[0].status == "active"


@pytest.mark.asyncio
async def test_invalid_uuids_are_not_found_not_errors(p3_admin_state):
    from app.db.models import get_model_row

    assert await get_model_row("not-a-uuid") is None


# --------------------------------------------------------------------------
# Catalogue seeding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_catalogue_seeds_defaults_and_fallbacks_and_is_idempotent(p3_admin_state, p3_pool):
    from app.providers.runtime_config import sync_catalogue

    rows = await p3_pool.fetch(
        "SELECT p.name AS provider, m.model_id, m.is_default, m.is_fallback, m.status FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id"
    )
    defaults = {r["provider"]: r["model_id"] for r in rows if r["is_default"]}
    assert defaults["grok"] == "grok-4.6" and defaults["mock"] == "mock-v1" and "gemma" in defaults
    assert not any(r["model_id"] == "custom-configured" for r in rows)  # the display placeholder is never seeded
    assert all(r["status"] == "retired" for r in rows if r["provider"] == "grok" and r["model_id"] in ("grok-3", "grok-4-fast"))
    assert await sync_catalogue() == []  # nothing new to offer


@pytest.mark.asyncio
async def test_a_deleted_seeded_row_stays_deleted_until_an_admin_forces_a_resync(p3_admin_state, p3_pool):
    from app.providers.runtime_config import sync_catalogue

    await p3_pool.execute("DELETE FROM ai_models WHERE model_id = 'grok-3'")
    assert await sync_catalogue() == []
    assert await p3_pool.fetchval("SELECT count(*) FROM ai_models WHERE model_id = 'grok-3'") == 0
    assert "grok:grok-3" in await sync_catalogue(force=True)
    assert await p3_pool.fetchval("SELECT count(*) FROM ai_models WHERE model_id = 'grok-3'") == 1


@pytest.mark.asyncio
async def test_a_resync_never_overrides_an_admins_default_but_fills_a_missing_one(p3_admin_state, p3_pool):
    from app.db.models import list_model_rows, update_model_row
    from app.providers.runtime_config import sync_catalogue

    grok = {r.model_id: r for r in await list_model_rows("grok")}
    await update_model_row(grok["grok-4.3"].id, is_default=True)  # the admin prefers 4.3
    await p3_pool.execute("UPDATE ai_models SET is_default = false, is_fallback = false WHERE provider_id = (SELECT id FROM ai_providers WHERE name = 'gemma')")
    await sync_catalogue(force=True)
    after = {r.model_id: r for r in await list_model_rows("grok")}
    assert after["grok-4.3"].is_default is True and after["grok-4.6"].is_default is False  # admin choice kept
    gemma = [r for r in await list_model_rows("gemma") if r.is_default]
    assert len(gemma) == 1  # a provider left without a default gets the catalogue's


@pytest.mark.asyncio
async def test_existing_rows_created_by_traffic_are_adopted_not_duplicated(p3_admin_state, p3_pool):
    from app.db.models import seed_missing

    provider = await p3_pool.fetchval("INSERT INTO ai_providers (name, type) VALUES ('mock', 'mock') ON CONFLICT (name) DO UPDATE SET name = 'mock' RETURNING id")
    await p3_pool.execute("UPDATE ai_models SET is_default = false WHERE provider_id = $1", provider)
    offered = await seed_missing(
        [{"provider": "mock", "model_id": "mock-v1", "display_name": "Ignored", "status": "active", "is_default": True}], set()
    )
    assert offered == ["mock:mock-v1"]
    row = await p3_pool.fetchrow("SELECT display_name, is_default FROM ai_models WHERE provider_id = $1 AND model_id = 'mock-v1'", provider)
    assert row["is_default"] is True and row["display_name"] != "Ignored"  # flagged, not renamed
    assert await p3_pool.fetchval("SELECT count(*) FROM ai_models WHERE provider_id = $1 AND model_id = 'mock-v1'", provider) == 1


# --------------------------------------------------------------------------
# Runtime configuration reloads
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_models_makes_the_database_the_registry(p3_admin_state):
    from app.db.models import create_model_row
    from app.providers.model_registry import configured_model_id, get_model_registry
    from app.providers.runtime_config import reload_models

    await create_model_row("custom", "zzp3-live", "Live", is_default=True)
    assert get_model_registry().loaded_from_db is False
    assert await reload_models() > 0
    registry = get_model_registry()
    assert registry.loaded_from_db is True and registry.default_model_id("custom") == "zzp3-live"
    assert configured_model_id("custom") == "zzp3-live"


@pytest.mark.asyncio
async def test_an_empty_table_never_blanks_the_registry(p3_admin_state, monkeypatch):
    import app.db.models as db_models
    from app.providers.model_registry import get_model_registry
    from app.providers.runtime_config import reload_models

    async def empty(provider=None):
        return []

    monkeypatch.setattr(db_models, "list_model_rows", empty)
    assert await reload_models() == 0
    assert get_model_registry().loaded_from_db is False and get_model_registry().default_model_id("grok") == "grok-4.6"


@pytest.mark.asyncio
async def test_reload_provider_settings_decrypts_keys_and_loads_flags(p3_admin_state, p3_pool, monkeypatch):
    from app.db.provider_settings import update_provider_flags, upsert_provider_settings
    from app.providers import provider_settings as ps
    from app.providers.runtime_config import reload_provider_settings
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    await upsert_provider_settings("grok", None, api_key_encrypted=ss.encrypt_secret("sk-admin-override-123"), timeout_ms=4000, retry_count=1)
    await update_provider_flags("grok", enabled=False, priority=7)
    await reload_provider_settings()
    ovr = ps.get_overrides("grok")
    assert (ovr.api_key, ovr.timeout_ms, ovr.retry_count, ovr.key_stored, ovr.key_error) == ("sk-admin-override-123", 4000, 1, True, None)
    assert ps.is_provider_enabled("grok") is False and ps.provider_priority("grok") == 7
    assert ps.is_provider_enabled("gemma") is True


@pytest.mark.asyncio
async def test_an_undecryptable_stored_key_is_ignored_loudly_and_the_environment_keeps_working(p3_admin_state, monkeypatch):
    from app.db.provider_settings import upsert_provider_settings
    from app.providers import provider_settings as ps
    from app.providers.grok_provider import GrokProvider
    from app.providers.runtime_config import reload_provider_settings
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    await upsert_provider_settings("grok", None, api_key_encrypted=ss.encrypt_secret("sk-stored-123456"))
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())  # the master key was replaced: old ciphertext is unreadable
    monkeypatch.setenv("XAI_API_KEY", "env-key-123456")
    await reload_provider_settings()
    ovr = ps.get_overrides("grok")
    assert ovr.api_key is None and ovr.key_stored is True and "could not be decrypted" in ovr.key_error
    assert ps.effective_api_key("grok", "XAI_API_KEY") == "env-key-123456" and GrokProvider().is_configured() is True


@pytest.mark.asyncio
async def test_stored_breaker_thresholds_are_applied_to_the_live_registry(p3_admin_state):
    from app.db.system_settings import set_setting
    from app.providers.circuit_breaker import get_circuit_breakers
    from app.providers.runtime_config import reload_breaker_settings

    await set_setting("circuit_breaker", {"failureThreshold": 2, "providers": {"gemma": {"cooldownSeconds": 9}}}, None)
    await reload_breaker_settings()
    breakers = get_circuit_breakers()
    grok, gemma = await breakers.snapshot("grok"), await breakers.snapshot("gemma")
    assert grok.failure_threshold == 2 and gemma.failure_threshold == 2 and gemma.cooldown_seconds == 9


@pytest.mark.asyncio
async def test_one_failing_section_does_not_stop_the_others(p3_admin_state, monkeypatch):
    import app.providers.runtime_config as rc

    async def broken():
        raise RuntimeError("models table unreadable")

    monkeypatch.setattr(rc, "reload_models", broken)
    report = await rc.reload_runtime_config()
    assert any("models: RuntimeError" in e for e in report["errors"])
    assert report["providerSettings"] >= 0 and len(report["errors"]) == 1


@pytest.mark.asyncio
async def test_reload_without_a_database_is_a_quiet_no_op(monkeypatch):
    from app.providers.runtime_config import reload_runtime_config

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert await reload_runtime_config(seed=True) == {"models": 0, "providerSettings": 0, "seeded": [], "errors": []}


# --------------------------------------------------------------------------
# Provider settings + system settings storage
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_upsert_touches_only_the_fields_passed_and_none_clears(p3_admin_state):
    from app.db.provider_settings import get_provider_settings, upsert_provider_settings

    await upsert_provider_settings("custom", None, base_url="http://a/v1", timeout_ms=2000, region="ap-south-1")
    before, after = await upsert_provider_settings("custom", None, timeout_ms=3000)
    assert before.base_url == "http://a/v1" and (after.base_url, after.timeout_ms, after.region) == ("http://a/v1", 3000, "ap-south-1")
    _, cleared = await upsert_provider_settings("custom", None, base_url=None, region=None)
    assert (cleared.base_url, cleared.region, cleared.timeout_ms) == (None, None, 3000)
    assert (await get_provider_settings("custom")).timeout_ms == 3000
    with pytest.raises(ValueError, match="unsupported"):
        await upsert_provider_settings("custom", None, nonsense=1)


@pytest.mark.asyncio
async def test_the_api_key_is_ciphertext_at_rest(p3_admin_state, p3_pool, monkeypatch):
    from app.db.provider_settings import upsert_provider_settings
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    secret = "sk-very-secret-value-0123456789"
    await upsert_provider_settings("grok", None, api_key_encrypted=ss.encrypt_secret(secret))
    stored = await p3_pool.fetchval("SELECT api_key_encrypted FROM ai_provider_settings s JOIN ai_providers p ON p.id = s.provider_id WHERE p.name = 'grok'")
    assert secret not in stored and "very-secret" not in stored and ss.decrypt_secret(stored) == secret


@pytest.mark.asyncio
async def test_system_settings_return_the_previous_value_for_the_audit_trail(p3_admin_state, p3_pool):
    from app.db.system_settings import delete_setting, get_setting, set_setting

    key = f"zzp3-{uuid.uuid4().hex[:6]}"
    try:
        assert await get_setting(key) is None
        assert await set_setting(key, {"a": 1}, None) is None
        assert await set_setting(key, {"a": 2}, None) == {"a": 1}
        assert await get_setting(key) == {"a": 2}
        assert await delete_setting(key) is True and await delete_setting(key) is False
    finally:
        await p3_pool.execute("DELETE FROM system_settings WHERE key = $1", key)


# --------------------------------------------------------------------------
# Lifecycle with a database: retirement is persisted, alerted and audited
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retiring_the_in_use_default_persists_alerts_audits_and_promotes(p3_admin_state, p3_pool):
    from app.providers.lifecycle import retire_model
    from app.providers.model_registry import configured_model_id, get_model_registry
    from app.providers.runtime_config import reload_models

    await reload_models()
    registry = get_model_registry()
    fallback = registry.fallback_model_id("grok")
    assert fallback and registry.default_model_id("grok") == "grok-4.6"

    outcome = await retire_model("grok", "grok-4.6", "xAI rejected the slug", detected_by="test")
    assert outcome.changed and outcome.persisted and outcome.was_in_use and outcome.replacement == fallback

    row = await p3_pool.fetchrow("SELECT m.status, m.is_default, m.config FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id WHERE p.name='grok' AND m.model_id='grok-4.6'")
    assert (row["status"], row["is_default"]) == ("retired", False)
    assert "xAI rejected the slug" in json.loads(row["config"])["retiredReason"]
    promoted = await p3_pool.fetchval("SELECT m.is_default FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id WHERE p.name='grok' AND m.model_id=$1", fallback)
    assert promoted is True
    assert configured_model_id("grok") == fallback  # the in-process registry was reloaded from the database

    alert = await p3_pool.fetchrow("SELECT severity, provider, message, status FROM error_logs WHERE type = 'ModelRetired' ORDER BY ts DESC LIMIT 1")
    assert (alert["severity"], alert["provider"], alert["status"]) == ("high", "grok", "open")
    assert "grok-4.6" in alert["message"] and fallback in alert["message"]
    audit = await p3_pool.fetchrow("SELECT actor_id, after FROM audit_logs WHERE action = 'model.auto_retire' ORDER BY ts DESC LIMIT 1")
    assert audit["actor_id"] is None and json.loads(audit["after"])["detectedBy"] == "test"

    again = await retire_model("grok", "grok-4.6", "again", detected_by="test")
    assert again.changed is False
    assert await p3_pool.fetchval("SELECT count(*) FROM error_logs WHERE type = 'ModelRetired'") == 1  # no duplicate alert


@pytest.mark.asyncio
async def test_retiring_an_unused_model_is_a_medium_alert_and_changes_no_default(p3_admin_state, p3_pool):
    from app.providers.lifecycle import retire_model
    from app.providers.model_registry import get_model_registry
    from app.providers.runtime_config import reload_models

    await reload_models()
    outcome = await retire_model("gemma", "gemma-4-26b-a4b-it", "not offered", detected_by="test")
    assert outcome.changed and outcome.was_in_use is False
    assert await p3_pool.fetchval("SELECT severity FROM error_logs WHERE type = 'ModelRetired' ORDER BY ts DESC LIMIT 1") == "medium"
    assert get_model_registry().default_model_id("gemma") is not None


@pytest.mark.asyncio
async def test_retiring_a_model_the_registry_never_recorded_creates_it_as_retired(p3_admin_state, p3_pool):
    from app.providers.lifecycle import retire_model

    outcome = await retire_model("custom", "zzp3-unknown", "vendor says gone", detected_by="test")
    assert outcome.changed
    assert await p3_pool.fetchval("SELECT m.status FROM ai_models m JOIN ai_providers p ON p.id=m.provider_id WHERE p.name='custom' AND m.model_id='zzp3-unknown'") == "retired"


# --------------------------------------------------------------------------
# Lifespan with a real database
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_seeds_and_loads_the_registry_and_starts_the_jobs(p3_admin_state, monkeypatch):
    import app.db.retention as retention
    import app.providers.availability as availability
    import app.providers.runtime_config as rc
    from app.config import get_settings
    from app.main import _lifespan, app
    from app.providers.model_registry import get_model_registry

    started: list[str] = []

    def fake(name):
        async def loop(*args, **kwargs):  # noqa: ANN002, ANN003
            started.append(name)
            await asyncio.sleep(3600)

        return loop

    monkeypatch.setattr(retention, "retention_loop", fake("retention"))
    monkeypatch.setattr(rc, "runtime_config_loop", fake("runtime-config"))
    monkeypatch.setattr(availability, "availability_loop", fake("model-availability"))
    get_settings(refresh=True)
    async with _lifespan(app):
        await asyncio.sleep(0.05)
        assert get_model_registry().loaded_from_db is True
    assert sorted(started) == ["model-availability", "retention", "runtime-config"]


@pytest.mark.asyncio
async def test_each_background_job_is_disabled_by_a_zero_interval(p3_admin_state, monkeypatch):
    import app.db.retention as retention
    import app.providers.availability as availability
    import app.providers.runtime_config as rc
    from app.config import get_settings
    from app.main import _lifespan, app

    called: list[str] = []

    async def loop(*args, **kwargs):  # noqa: ANN002, ANN003
        called.append("x")
        await asyncio.sleep(3600)

    for module, name in ((retention, "retention_loop"), (rc, "runtime_config_loop"), (availability, "availability_loop")):
        monkeypatch.setattr(module, name, loop)
    for env in ("RETENTION_INTERVAL_SECONDS", "RUNTIME_CONFIG_REFRESH_SECONDS", "MODEL_AVAILABILITY_INTERVAL_SECONDS"):
        monkeypatch.setenv(env, "0")
    get_settings(refresh=True)
    try:
        async with _lifespan(app):
            await asyncio.sleep(0.05)
        assert called == []
    finally:
        for env in ("RETENTION_INTERVAL_SECONDS", "RUNTIME_CONFIG_REFRESH_SECONDS", "MODEL_AVAILABILITY_INTERVAL_SECONDS"):
            monkeypatch.delenv(env, raising=False)
        get_settings(refresh=True)


@pytest.mark.asyncio
async def test_the_refresh_loop_picks_up_a_change_made_by_another_instance(p3_admin_state, p3_pool):
    from app.providers.model_registry import get_model_registry
    from app.providers.runtime_config import reload_models, runtime_config_loop

    await reload_models()
    await p3_pool.execute("UPDATE ai_models SET display_name = 'Changed elsewhere' WHERE model_id = 'mock-v1'")
    task = asyncio.create_task(runtime_config_loop(0.05))
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            entry = get_model_registry().get("mock", "mock-v1")
            if entry and entry.display_name == "Changed elsewhere":
                break
        else:
            pytest.fail("the refresh loop never picked up the change")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
