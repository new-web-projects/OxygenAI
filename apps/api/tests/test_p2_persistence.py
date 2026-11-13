"""
Phase P2 — persistence hygiene and operational foundations, against a real
Postgres (skipped without DATABASE_URL, like the other integration modules).

What these prove, each against live rows:

* `technical_indicators` is idempotent per (instrument, ts, timeframe,
  indicator) and migration 0006 removes pre-existing duplicates;
* `audit_logs`, `error_logs` and `ai_health_checks` now have writers, wired to
  the actions that should leave a trail;
* multi-provider comparisons write `usage_logs` for every provider that
  answered;
* an `ai_models` display name is never overwritten by ordinary traffic;
* the retention job deletes expired sessions/memories and nothing else.

Every row created here is prefixed ZZP2/zzp2 and removed in teardown.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — P2 persistence tests require a live Postgres instance",
)

PASSWORD = "correct-horse-battery-staple"
MIGRATION_0006 = Path(__file__).resolve().parents[3] / "infra" / "migrations" / "0006_persistence_hygiene.sql"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
    from app.config import get_settings

    get_settings(refresh=True)

    import app.db.client as db_client

    db_client._pool = None

    import httpx

    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c

    db_client._pool = None
    get_settings(refresh=True)


@pytest.fixture
async def pool():
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    p = await get_pool()
    yield p
    db_client._pool = None


@pytest.fixture
async def accounts(pool):
    created: list[str] = []

    async def make(client, *, role: str = "user") -> tuple[str, str, dict[str, str]]:
        email = f"zzp2-{uuid.uuid4().hex[:12]}@example.com"
        created.append(email)
        registered = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 200, registered.text
        if role != "user":
            from app.db.users import set_user_role

            assert await set_user_role(email, role)
        user_id = str(await pool.fetchval("SELECT id FROM users WHERE email = $1", email))
        return email, user_id, {"Authorization": f"Bearer {registered.json()['accessToken']}"}

    yield make

    for email in created:
        sub = "(SELECT id FROM users WHERE email = $1)"
        await pool.execute(f"DELETE FROM audit_logs WHERE actor_id IN {sub}", email)
        await pool.execute(f"DELETE FROM conversations WHERE user_id IN {sub}", email)
        await pool.execute(f"DELETE FROM usage_logs WHERE user_id IN {sub}", email)
        await pool.execute(f"DELETE FROM error_logs WHERE user_id IN {sub}", email)
        await pool.execute("DELETE FROM users WHERE email = $1", email)


async def _drop_instrument(pool, symbol: str) -> None:
    for table in ("trading_signals", "technical_indicators", "market_data"):
        await pool.execute(
            f"DELETE FROM {table} WHERE instrument_id IN (SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
    await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


# --------------------------------------------------------------------------
# technical_indicators — unique key + upsert + migration dedupe
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisting_the_same_bundle_twice_does_not_duplicate_rows(pool):
    from app.db.indicators_db import persist_indicator_bundle
    from app.db.market_data import get_or_create_instrument
    from app.indicators import compute_indicators, generate_synthetic_ohlcv

    symbol = "ZZP2IND"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        bars = generate_synthetic_ohlcv(symbol, 60)
        bundle = compute_indicators(bars)
        ts = bars[-1].timestamp

        await persist_indicator_bundle(instrument_id, ts, "1d", bundle)
        first = await pool.fetchval("SELECT count(*) FROM technical_indicators WHERE instrument_id = $1", instrument_id)
        assert first > 0
        for _ in range(3):
            await persist_indicator_bundle(instrument_id, ts, "1d", bundle)
        assert await pool.fetchval("SELECT count(*) FROM technical_indicators WHERE instrument_id = $1", instrument_id) == first
    finally:
        await _drop_instrument(pool, symbol)


@pytest.mark.asyncio
async def test_re_persisting_updates_the_stored_value_in_place(pool):
    import json

    from app.db.indicators_db import persist_indicator_bundle
    from app.db.market_data import get_or_create_instrument
    from app.indicators import compute_indicators, generate_synthetic_ohlcv

    symbol = "ZZP2UPD"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        bars = generate_synthetic_ohlcv(symbol, 60)
        bundle = compute_indicators(bars)
        ts = bars[-1].timestamp

        await persist_indicator_bundle(instrument_id, ts, "1d", bundle)
        await persist_indicator_bundle(instrument_id, ts, "1d", bundle.model_copy(update={"rsi14": 12.5}))

        rows = await pool.fetch(
            "SELECT value FROM technical_indicators WHERE instrument_id = $1 AND indicator_name = 'rsi14'", instrument_id
        )
        assert len(rows) == 1
        assert json.loads(rows[0]["value"]) == 12.5
    finally:
        await _drop_instrument(pool, symbol)


@pytest.mark.asyncio
async def test_migration_0006_removes_existing_duplicates_and_is_idempotent(pool):
    """
    Runs the real migration file inside a transaction that is rolled back, so the
    shared database is untouched: drop the unique index, plant duplicates the way
    pre-P2 code produced them, apply the migration, check, roll back.
    """
    from app.db.market_data import get_or_create_instrument

    symbol = "ZZP2MIG"
    instrument_id = await get_or_create_instrument(symbol)
    sql = MIGRATION_0006.read_text()
    ts = datetime(2026, 1, 2, tzinfo=timezone.utc)
    try:
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute("DROP INDEX IF EXISTS uq_technical_indicators_key")
                for value in ("1.0", "2.0", "3.0"):  # three copies of one key; the last (newest id) must survive
                    await conn.execute(
                        "INSERT INTO technical_indicators (instrument_id, ts, timeframe, indicator_name, value) "
                        "VALUES ($1, $2, '1d', 'rsi14', $3::jsonb)",
                        instrument_id, ts, value,
                    )
                await conn.execute(
                    "INSERT INTO technical_indicators (instrument_id, ts, timeframe, indicator_name, value) "
                    "VALUES ($1, $2, '1d', 'atr14', '9.0'::jsonb)", instrument_id, ts,
                )

                await conn.execute(sql)
                rows = await conn.fetch(
                    "SELECT indicator_name, value::text AS v FROM technical_indicators WHERE instrument_id = $1 ORDER BY indicator_name",
                    instrument_id,
                )
                assert [(r["indicator_name"], r["v"]) for r in rows] == [("atr14", "9.0"), ("rsi14", "3.0")]

                await conn.execute(sql)  # idempotent: a second run changes nothing and does not error
                assert await conn.fetchval("SELECT count(*) FROM technical_indicators WHERE instrument_id = $1", instrument_id) == 2

                with pytest.raises(Exception, match="uq_technical_indicators_key"):
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO technical_indicators (instrument_id, ts, timeframe, indicator_name, value) "
                            "VALUES ($1, $2, '1d', 'rsi14', '4.0'::jsonb)", instrument_id, ts,
                        )
            finally:
                await tx.rollback()
        assert await pool.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_technical_indicators_key'") == 1
    finally:
        await _drop_instrument(pool, symbol)


@pytest.mark.asyncio
async def test_migration_0006_repairs_corrupted_display_names_only(pool):
    from app.db.signals import get_or_seed_provider

    sql = MIGRATION_0006.read_text()
    provider_id = await get_or_seed_provider("mock")
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("UPDATE ai_models SET display_name = 'Grok (xAI)' WHERE provider_id = $1 AND model_id = 'mock-v1'", provider_id)
            await conn.execute(
                "INSERT INTO ai_models (provider_id, model_id, display_name) VALUES ($1, 'zzp2-named-like-provider', 'mock')", provider_id
            )
            await conn.execute(
                "INSERT INTO ai_models (provider_id, model_id, display_name) VALUES ($1, 'zzp2-admin-named', 'Chosen By An Admin')", provider_id
            )
            await conn.execute(sql)
            names = {
                r["model_id"]: r["display_name"]
                for r in await conn.fetch("SELECT model_id, display_name FROM ai_models WHERE provider_id = $1", provider_id)
            }
            assert names.get("mock-v1", "Mock Reasoner (offline)") == "Mock Reasoner (offline)"
            assert names["zzp2-named-like-provider"] == "zzp2-named-like-provider"
            assert names["zzp2-admin-named"] == "Chosen By An Admin"  # a deliberate name is never touched
        finally:
            await tx.rollback()


# --------------------------------------------------------------------------
# audit_logs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_audit_writes_actor_before_and_after(client, accounts, pool):
    from app.db.audit import record_audit

    _, user_id, _ = await accounts(client, role="admin")
    resource = f"zzp2:{uuid.uuid4().hex[:8]}"
    assert await record_audit(actor_id=user_id, action="zzp2.update", resource=resource, before={"a": 1}, after={"a": 2, "when": datetime(2026, 1, 1)})
    assert await record_audit(actor_id=None, action="zzp2.system", resource=resource)

    import json

    rows = await pool.fetch("SELECT actor_id, action, before, after FROM audit_logs WHERE resource = $1 ORDER BY ts", resource)
    try:
        assert [r["action"] for r in rows] == ["zzp2.update", "zzp2.system"]
        assert str(rows[0]["actor_id"]) == user_id and rows[1]["actor_id"] is None
        assert json.loads(rows[0]["before"]) == {"a": 1}
        assert json.loads(rows[0]["after"])["a"] == 2  # a non-JSON value (datetime) was serialised, not fatal
        assert rows[1]["before"] is None and rows[1]["after"] is None
    finally:
        await pool.execute("DELETE FROM audit_logs WHERE resource = $1", resource)


@pytest.mark.asyncio
async def test_a_failed_audit_write_returns_false_instead_of_raising(pool):
    from app.db.audit import record_audit

    # actor_id is a uuid column: a malformed value makes the INSERT fail.
    assert await record_audit(actor_id="not-a-uuid", action="zzp2.bad", resource="zzp2:bad") is False


@pytest.mark.asyncio
async def test_knowledge_upload_delete_and_tool_invoke_leave_an_audit_trail(client, accounts, pool):
    _, admin_id, admin = await accounts(client, role="admin")
    uploaded = await client.post(
        "/api/knowledge/upload",
        json={"title": "zzp2 note", "content": "Position sizing must cap risk per trade.", "sourceType": "knowledge"},
        headers=admin,
    )
    assert uploaded.status_code == 200, uploaded.text
    document_id = uploaded.json()["documentId"]
    try:
        invoked = await client.post(
            "/api/ai/tools/knowledge_retrieval/invoke", headers=admin, json={"query": "risk per trade", "topK": 1}
        )
        assert invoked.status_code == 200, invoked.text
        assert (await client.delete(f"/api/knowledge/documents/{document_id}", headers=admin)).status_code == 200

        rows = await pool.fetch("SELECT action, resource FROM audit_logs WHERE actor_id = $1 ORDER BY ts", admin_id)
        assert [r["action"] for r in rows] == ["knowledge.upload", "tool.invoke", "knowledge.delete"]
        assert rows[0]["resource"] == rows[2]["resource"] == f"knowledge_document:{document_id}"
        assert rows[1]["resource"] == "tool:knowledge_retrieval"
    finally:
        await pool.execute("DELETE FROM knowledge_documents WHERE id = $1", document_id)


@pytest.mark.asyncio
async def test_cli_role_changes_are_audited_with_a_null_actor(pool):
    from app.cli import _create_admin, _set_role
    from app.db.client import get_pool

    email = f"zzp2-cli-{uuid.uuid4().hex[:8]}@example.com"
    user_id = None
    try:
        await _create_admin(email, PASSWORD)
        p = await get_pool()
        user_id = str(await p.fetchval("SELECT id FROM users WHERE email = $1", email))
        await _set_role(email, "user")
        p = await get_pool()
        rows = await p.fetch("SELECT actor_id, action, after FROM audit_logs WHERE resource = $1 ORDER BY ts", f"user:{user_id}")
        assert [r["action"] for r in rows] == ["user.create_admin", "user.role_set"]
        assert all(r["actor_id"] is None for r in rows)
    finally:
        p = await get_pool()
        if user_id:
            await p.execute("DELETE FROM audit_logs WHERE resource = $1", f"user:{user_id}")
        await p.execute("DELETE FROM users WHERE email = $1", email)


# --------------------------------------------------------------------------
# error_logs
# --------------------------------------------------------------------------


@pytest.fixture
def exploding_app():
    from fastapi import FastAPI

    from app.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("zzp2 deliberate failure \x00 with a NUL")

    return app


@pytest.mark.asyncio
async def test_the_error_id_in_a_500_response_is_a_durable_error_logs_row(exploding_app, pool):
    import httpx

    transport = httpx.ASGITransport(app=exploding_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    error_id = response.json()["errorId"]
    try:
        row = await pool.fetchrow("SELECT * FROM error_logs WHERE error_id = $1", error_id)
        assert row is not None
        assert row["type"] == "RuntimeError"
        assert row["route"] == "GET /boom"
        assert row["severity"] == "high" and row["status"] == "open"
        assert "zzp2 deliberate failure" in row["message"] and "\x00" not in row["message"]
        assert "Traceback" in row["stack"]
        assert row["user_id"] is None
    finally:
        await pool.execute("DELETE FROM error_logs WHERE error_id = $1", error_id)


@pytest.mark.asyncio
async def test_a_500_from_an_authenticated_caller_records_who_hit_it(client, accounts, exploding_app, pool):
    import httpx

    _, user_id, headers = await accounts(client)
    transport = httpx.ASGITransport(app=exploding_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/boom", headers=headers)
    error_id = response.json()["errorId"]
    try:
        assert str(await pool.fetchval("SELECT user_id FROM error_logs WHERE error_id = $1", error_id)) == user_id
    finally:
        await pool.execute("DELETE FROM error_logs WHERE error_id = $1", error_id)


@pytest.mark.asyncio
async def test_a_token_for_a_deleted_account_does_not_lose_the_error_row(client, exploding_app, pool):
    import httpx

    from app.security.jwt import create_access_token

    # Validly signed, but the account does not exist: the foreign key would reject
    # the row if the id were inserted as-is; the writer must null it instead.
    token, _ = create_access_token(str(uuid.uuid4()), "ghost@example.com", "user")
    transport = httpx.ASGITransport(app=exploding_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/boom", headers={"Authorization": f"Bearer {token}"})
    error_id = response.json()["errorId"]
    try:
        row = await pool.fetchrow("SELECT user_id FROM error_logs WHERE error_id = $1", error_id)
        assert row is not None and row["user_id"] is None
    finally:
        await pool.execute("DELETE FROM error_logs WHERE error_id = $1", error_id)


# --------------------------------------------------------------------------
# ai_health_checks
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_probes_are_persisted(client, accounts, pool, monkeypatch):
    for name in ("XAI_API_KEY", "CUSTOM_AI_BASE_URL", "CUSTOM_AI_MODEL_ID", "CUSTOM_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _, _, admin = await accounts(client, role="admin")
    started = await pool.fetchval("SELECT now()")
    try:
        assert (await client.post("/api/ai/mock/test", headers=admin)).json()["ok"] is True
        assert (await client.get("/api/ai/grok/health", headers=admin)).json()["ok"] is False
        assert (await client.get("/api/ai/custom/health", headers=admin)).json()["ok"] is False
        assert (await client.get("/api/ai/nope/health", headers=admin)).status_code == 404

        rows = await pool.fetch(
            """
            SELECT p.name AS provider, h.status, h.latency_ms, h.error_detail, h.model_id
              FROM ai_health_checks h JOIN ai_providers p ON p.id = h.provider_id
             WHERE h.checked_at >= $1 ORDER BY h.checked_at
            """,
            started,
        )
        by_provider = {r["provider"]: r for r in rows}
        assert [r["provider"] for r in rows] == ["mock", "grok", "custom"]  # the 404 wrote nothing
        assert by_provider["mock"]["status"] == "ok" and by_provider["mock"]["error_detail"] is None
        assert by_provider["mock"]["model_id"] is not None
        assert by_provider["grok"]["status"] == "down" and by_provider["grok"]["error_detail"]
        assert by_provider["custom"]["status"] == "down"
        assert by_provider["custom"]["model_id"] is None  # an unconfigured "unset" model is not invented as a row
    finally:
        await pool.execute("DELETE FROM ai_health_checks WHERE checked_at >= $1", started)


# --------------------------------------------------------------------------
# usage_logs in multi-provider mode + ai_models naming
# --------------------------------------------------------------------------


@pytest.fixture
def custom_provider_online(monkeypatch):
    """Configures Custom AI against an endpoint that is never contacted: `reason` is replaced."""
    from app.providers.base import ProviderReasoning
    from app.providers.custom_ai_provider import CustomAIProvider

    monkeypatch.setenv("CUSTOM_AI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("CUSTOM_AI_MODEL_ID", "zzp2-custom-model")
    monkeypatch.delenv("CUSTOM_AI_API_KEY", raising=False)

    async def reason(self, context, model_id=None):  # noqa: ANN001
        return ProviderReasoning(direction=None, confidence=None, reasoning_summary="declined", supporting_evidence=[])

    monkeypatch.setattr(CustomAIProvider, "reason", reason)


@pytest.mark.asyncio
async def test_multi_provider_comparison_writes_usage_for_every_provider_that_answered(
    client, accounts, pool, custom_provider_online
):
    _, user_id, headers = await accounts(client)
    symbol = "ZZP2USAGE"
    try:
        response = await client.post(
            "/api/ai/analyze", json={"symbol": symbol, "providers": ["mock", "custom", "grok"]}, headers=headers
        )
        assert response.status_code == 200, response.text
        outcomes = {r["providerId"]: r["outcome"] for r in response.json()["results"]}
        assert outcomes == {"mock": "ok", "custom": "ok", "grok": "unavailable"}
        assert response.json()["comparisonId"]

        rows = await pool.fetch(
            """
            SELECT p.name AS provider, m.model_id, m.display_name
              FROM usage_logs u
              JOIN ai_providers p ON p.id = u.provider_id
              JOIN ai_models m ON m.id = u.model_id
             WHERE u.user_id = $1 ORDER BY p.name
            """,
            user_id,
        )
        assert [(r["provider"], r["model_id"]) for r in rows] == [("custom", "zzp2-custom-model"), ("mock", "mock-v1")]
        assert {r["provider"]: r["display_name"] for r in rows}["mock"] == "Mock Reasoner (offline)"
    finally:
        await _drop_instrument(pool, symbol)
        await pool.execute(
            "DELETE FROM ai_models WHERE model_id = 'zzp2-custom-model' AND provider_id IN (SELECT id FROM ai_providers WHERE name = 'custom')"
        )


@pytest.mark.asyncio
async def test_a_fallback_never_renames_the_mock_model_row(client, accounts, pool, monkeypatch):
    from app.db.signals import get_or_seed_model
    from app.providers.grok_provider import GrokProvider

    monkeypatch.setenv("XAI_API_KEY", "zzp2-not-a-real-key")

    async def broken(self, context, model_id=None):  # noqa: ANN001
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(GrokProvider, "reason", broken)

    seeded = await get_or_seed_model("mock", "mock-v1")
    original = await pool.fetchval("SELECT display_name FROM ai_models WHERE id = $1", seeded.model_db_id)
    await pool.execute("UPDATE ai_models SET display_name = 'zzp2 sentinel name' WHERE id = $1", seeded.model_db_id)
    _, _, headers = await accounts(client)
    symbol = "ZZP2FALL"
    try:
        response = await client.post("/api/ai/analyze", json={"symbol": symbol, "provider": "grok"}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["provider"], body["source"]) == ("mock", "fallback")  # the fallback really happened
        assert await pool.fetchval("SELECT display_name FROM ai_models WHERE id = $1", seeded.model_db_id) == "zzp2 sentinel name"
    finally:
        await pool.execute("UPDATE ai_models SET display_name = $2 WHERE id = $1", seeded.model_db_id, original)
        await _drop_instrument(pool, symbol)


def test_default_display_name_comes_from_the_catalogue_for_known_models_else_the_model_id():
    from app.db.signals import _default_display_name
    from app.providers.model_registry import get_model_registry

    entry = get_model_registry().get("grok", "grok-4.6")
    assert entry is not None
    assert _default_display_name("grok", "grok-4.6") == entry.display_name
    assert _default_display_name("mock", "mock-v1") == "Mock Reasoner (offline)"
    assert _default_display_name("grok", "zzp2-unlisted-model") == "zzp2-unlisted-model"


@pytest.mark.asyncio
async def test_a_new_unlisted_model_row_is_named_by_its_model_id_and_keeps_that_name(pool):
    from app.db.signals import get_or_seed_model

    first = await get_or_seed_model("grok", "zzp2-unlisted-model")
    try:
        await get_or_seed_model("grok", "zzp2-unlisted-model", "A Later Caller's Label")
        assert (
            await pool.fetchval("SELECT display_name FROM ai_models WHERE id = $1", first.model_db_id)
            == "zzp2-unlisted-model"
        )
    finally:
        await pool.execute("DELETE FROM ai_models WHERE id = $1", first.model_db_id)


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_purges_only_expired_rows(client, accounts, pool):
    from app.db.retention import run_retention_once

    _, user_id, _ = await accounts(client)
    now = datetime.now(timezone.utc)

    async def session(name: str, delta: timedelta) -> str:
        return str(await pool.fetchval(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES ($1, $2, $3) RETURNING id",
            user_id, f"zzp2-{name}-{uuid.uuid4().hex}", now + delta,
        ))

    async def memory(key: str, delta: timedelta | None) -> str:
        return str(await pool.fetchval(
            "INSERT INTO memories (user_id, layer, key, value, expires_at) VALUES ($1, 'session', $2, '{}'::jsonb, $3) RETURNING id",
            user_id, key, None if delta is None else now + delta,
        ))

    expired_session = await session("old", timedelta(minutes=-5))
    live_session = await session("live", timedelta(hours=1))
    expired_memory = await memory("old", timedelta(minutes=-5))
    live_memory = await memory("live", timedelta(hours=1))
    forever_memory = await memory("forever", None)

    report = await run_retention_once()
    assert report.errors == 0
    assert report.sessions_deleted >= 1 and report.memories_deleted >= 1

    remaining_sessions = {str(r["id"]) for r in await pool.fetch("SELECT id FROM sessions WHERE user_id = $1", user_id)}
    remaining_memories = {str(r["id"]) for r in await pool.fetch("SELECT id FROM memories WHERE user_id = $1", user_id)}
    assert expired_session not in remaining_sessions and live_session in remaining_sessions
    assert expired_memory not in remaining_memories
    assert {live_memory, forever_memory} <= remaining_memories  # long-term memory (NULL expiry) is never purged

    after_first = {str(r["id"]) for r in await pool.fetch("SELECT id FROM sessions WHERE user_id = $1", user_id)}
    again = await run_retention_once()  # idempotent: nothing of ours is left to delete
    assert again.errors == 0
    assert {str(r["id"]) for r in await pool.fetch("SELECT id FROM sessions WHERE user_id = $1", user_id)} == after_first


@pytest.mark.asyncio
async def test_retention_never_touches_audit_logs_or_trade_results(pool):
    from app.db.audit import record_audit
    from app.db.retention import run_retention_once

    resource = f"zzp2:{uuid.uuid4().hex[:8]}"
    await record_audit(actor_id=None, action="zzp2.keep", resource=resource)
    try:
        await run_retention_once()
        assert await pool.fetchval("SELECT count(*) FROM audit_logs WHERE resource = $1", resource) == 1
    finally:
        await pool.execute("DELETE FROM audit_logs WHERE resource = $1", resource)


@pytest.mark.asyncio
async def test_cli_purge_expired_reports_what_it_deleted(pool):
    from app.cli import _purge_expired

    message = await _purge_expired()
    assert message.startswith("Deleted ") and "expired session(s)" in message and "expired memory row(s)" in message


@pytest.mark.asyncio
async def test_a_failing_purge_step_is_counted_and_does_not_block_the_other(monkeypatch, pool):
    import app.db.retention as retention

    async def broken():
        raise RuntimeError("table is locked")

    monkeypatch.setattr(retention, "purge_expired_sessions", broken)
    report = await retention.run_retention_once()
    assert report.errors == 1
    assert report.sessions_deleted == 0
    assert report.memories_deleted >= 0
