"""
Regression tests for: POST /api/knowledge/search -> HTTP 500,
"ValueError: Out of range float values are not JSON compliant: nan".

Root cause: text with no ASCII letter/digit (a Bengali document, a
punctuation-only query, ...) embedded to an all-zero vector; pgvector's
cosine distance against a zero vector is NaN; the NaN similarity could not
be serialised. These tests drive the REAL application end to end (ASGI
transport, real Postgres + pgvector, real hashing embedder) using only the
public API plus raw SQL, so each scenario is exactly what a user hits.

Skipped without DATABASE_URL, like the other integration modules.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from .knowledge_support import (
    TITLE_PREFIX,
    ZERO_VECTOR_LITERAL,
    delete_test_documents,
    insert_legacy_chunk,
    insert_ready_document,
)
from .strict_json import assert_all_finite, loads_strict

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — knowledge search tests require a live Postgres instance with pgvector",
)

PASSWORD = "correct-horse-battery-staple"
ASCII_TEXT = "RSI measures momentum. A reading above 70 is overbought and below 30 is oversold."
BENGALI_TEXT = "ভারতীয় শেয়ার বাজার বিশ্লেষণ এবং ঝুঁকি ব্যবস্থাপনা"


@pytest.fixture
async def pool():
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    p = await get_pool()
    yield p
    await delete_test_documents(p)
    db_client._pool = None


@pytest.fixture
async def client(monkeypatch, pool):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)  # pin the deterministic hashing embedder
    from app.config import get_settings

    get_settings(refresh=True)

    from app.main import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings(refresh=True)


@pytest.fixture
async def admin(client, pool):
    """An admin account's auth headers (role set in SQL, independent of any app code)."""
    email = f"zztest-{uuid.uuid4().hex[:12]}@example.com"
    registered = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 200, registered.text
    await pool.execute(
        "UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'admin') WHERE email = $1", email
    )
    yield {"Authorization": f"Bearer {registered.json()['accessToken']}"}
    await pool.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email)
    await pool.execute("DELETE FROM users WHERE email = $1", email)


def _title(label: str) -> str:
    return f"{TITLE_PREFIX}{label}-{uuid.uuid4().hex[:6]}"


async def _upload(client, admin, title: str, content: str):
    return await client.post(
        "/api/knowledge/upload",
        headers=admin,
        json={"title": title, "content": content, "sourceType": "knowledge"},
    )


async def _search(client, admin, query: str, top_k: int = 10):
    response = await client.post("/api/knowledge/search", headers=admin, json={"query": query, "topK": top_k})
    return response


def _ours(hits: list[dict]) -> list[dict]:
    """Only the documents this test created (the database may hold others)."""
    return [h for h in hits if h["documentTitle"].startswith(TITLE_PREFIX)]


# --------------------------------------------------------------------------
# The reported failure, reproduced through the public API
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_succeeds_when_the_corpus_contains_non_latin_text(client, admin):
    ascii_title, bengali_title = _title("ascii"), _title("bengali")
    assert (await _upload(client, admin, ascii_title, ASCII_TEXT)).status_code == 200
    # The upload always "succeeded" — it was the SEARCH that failed afterwards.
    uploaded = await _upload(client, admin, bengali_title, BENGALI_TEXT)
    assert uploaded.status_code == 200 and uploaded.json()["chunkCount"] >= 1

    response = await _search(client, admin, "RSI momentum")

    assert response.status_code == 200, response.text  # was: 500, nan is not JSON compliant
    hits = loads_strict(response.text)  # strict: rejects NaN / Infinity
    assert_all_finite(hits)
    ours = _ours(hits)
    assert [h["documentTitle"] for h in ours][0] == ascii_title
    assert {h["documentTitle"] for h in ours} == {ascii_title, bengali_title}


@pytest.mark.asyncio
async def test_a_non_latin_query_finds_the_matching_document(client, admin):
    ascii_title, bengali_title = _title("ascii"), _title("bengali")
    await _upload(client, admin, ascii_title, ASCII_TEXT)
    await _upload(client, admin, bengali_title, BENGALI_TEXT)

    response = await _search(client, admin, "শেয়ার বাজার")  # a Bengali query: previously a zero query vector

    assert response.status_code == 200, response.text
    hits = _ours(loads_strict(response.text))
    assert hits[0]["documentTitle"] == bengali_title
    assert hits[0]["score"] > 0.3  # a genuine lexical match, not a degenerate 0


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["???", "--- !!!", "😀", "   "])
async def test_a_query_with_nothing_to_search_on_returns_an_empty_list(client, admin, query):
    # The corpus must be non-empty: a zero query vector yields a NaN
    # similarity against every stored row, and with no rows there is
    # nothing to go wrong (an earlier draft of this test passed on the
    # broken code for exactly that reason).
    await _upload(client, admin, _title("ascii"), ASCII_TEXT)

    response = await _search(client, admin, query)
    assert response.status_code == 200, response.text  # was: 500
    assert loads_strict(response.text) == []


@pytest.mark.asyncio
async def test_uploading_text_with_nothing_indexable_is_rejected_cleanly(client, admin, pool):
    title = _title("symbols")

    response = await _upload(client, admin, title, "??? --- !!! 😀😀")

    # Before: accepted (200) and stored a zero vector that broke every later search.
    assert response.status_code == 400, response.text
    assert "indexed" in response.json()["error"]
    status = await pool.fetchval("SELECT status FROM knowledge_documents WHERE title = $1", title)
    assert status == "failed"  # not left 'processing', not 'ready'


# --------------------------------------------------------------------------
# Data written by the earlier version — the state a real database is in now
# --------------------------------------------------------------------------


async def _seed_legacy_poison(pool) -> str:
    title = _title("legacy-poison")
    document_id = await insert_ready_document(pool, title)
    await insert_legacy_chunk(pool, document_id, 0, BENGALI_TEXT, ZERO_VECTOR_LITERAL)
    return title


@pytest.mark.asyncio
async def test_search_survives_zero_vector_rows_left_by_older_versions(client, admin, pool):
    ascii_title = _title("ascii")
    await _upload(client, admin, ascii_title, ASCII_TEXT)
    poisoned_title = await _seed_legacy_poison(pool)

    response = await _search(client, admin, "RSI momentum")

    # The tokenizer fix alone cannot help here: the stored vector is already
    # zero. The SQL-level exclusion is what keeps this 200.
    assert response.status_code == 200, response.text
    hits = loads_strict(response.text)
    assert_all_finite(hits)
    titles = {h["documentTitle"] for h in _ours(hits)}
    assert ascii_title in titles
    assert poisoned_title not in titles  # unrankable rows are excluded, not returned as NaN


@pytest.mark.asyncio
async def test_analysis_and_the_knowledge_tool_are_unaffected_by_poisoned_rows(client, admin, pool):
    ascii_title = _title("ascii")
    await _upload(client, admin, ascii_title, ASCII_TEXT)
    poisoned_title = await _seed_legacy_poison(pool)

    analysis = await client.post("/api/ai/analyze", headers=admin, json={"symbol": "ZZNAN", "provider": "mock"})
    assert analysis.status_code == 200, analysis.text
    body = loads_strict(analysis.text)
    assert_all_finite(body)
    assert not any(poisoned_title in source for source in body.get("knowledgeSources", []))

    tool = await client.post(
        "/api/ai/tools/knowledge_retrieval/invoke", headers=admin, json={"query": "RSI momentum", "topK": 10}
    )
    assert tool.status_code == 200, tool.text
    outcome = loads_strict(tool.text)
    assert outcome["status"] == "ok", outcome
    assert_all_finite(outcome)
    assert poisoned_title not in {r["documentTitle"] for r in outcome["output"]["results"]}
