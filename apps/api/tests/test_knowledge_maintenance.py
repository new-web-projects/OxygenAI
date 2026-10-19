"""
`python -m app.rag.maintenance reembed-unusable` — repair of zero-vector rows
written by versions that did not validate embeddings.

These tests only ever touch the documents they create (the command is run
with a document scope), so they are safe to run against a development
database that holds real knowledge — including real poisoned rows, which
are exactly what the command exists to repair.

The "no database" test needs more care than removing DATABASE_URL from the
subprocess environment: `load_env_files()` re-reads `.env.local` / `.env`
from the package's own location (apps/api and the repo root) and fills in
anything the real environment lacks, so on a machine with a real
`.env.local` the command finds a database after all — and, being unscoped,
ran a real repair against it. That test therefore runs against an isolated
copy of the package whose only env files are ones the test wrote itself.

Skipped without DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.db.knowledge import list_unusable_embeddings, similarity_search
from app.rag.embeddings import HashingEmbeddingProvider
from app.rag.maintenance import reembed_unusable

from .knowledge_support import (
    TITLE_PREFIX,
    ZERO_VECTOR_LITERAL,
    delete_test_documents,
    insert_legacy_chunk,
    insert_ready_document,
    vector_literal,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — maintenance tests require a live Postgres instance with pgvector",
)

API_ROOT = Path(__file__).resolve().parents[1]
BENGALI = "ভারতীয় শেয়ার বাজার বিশ্লেষণ এবং ঝুঁকি ব্যবস্থাপনা"
HOPELESS = "?! 😀 ---"
ASCII = "RSI measures momentum and overbought conditions"


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)  # deterministic hashing embedder
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    p = await get_pool()
    yield p
    await delete_test_documents(p)
    db_client._pool = None


async def _seed_legacy_document(pool) -> dict:
    """A document as an unvalidated version would have left it: one
    recoverable zero-vector chunk, one hopeless one, one healthy one."""
    title = f"{TITLE_PREFIX}maint-{uuid.uuid4().hex[:6]}"
    document_id = await insert_ready_document(pool, title)
    healthy_vector = await HashingEmbeddingProvider().embed(ASCII)
    recoverable_chunk, recoverable_embedding = await insert_legacy_chunk(pool, document_id, 0, BENGALI, ZERO_VECTOR_LITERAL)
    hopeless_chunk, hopeless_embedding = await insert_legacy_chunk(pool, document_id, 1, HOPELESS, ZERO_VECTOR_LITERAL)
    healthy_chunk, healthy_embedding = await insert_legacy_chunk(pool, document_id, 2, ASCII, vector_literal(healthy_vector))
    return {
        "document_id": document_id,
        "recoverable": (recoverable_chunk, recoverable_embedding),
        "hopeless": (hopeless_chunk, hopeless_embedding),
        "healthy": (healthy_chunk, healthy_embedding),
        "healthy_vector": healthy_vector,
    }


async def _vector_text(pool, embedding_id: str) -> str:
    return await pool.fetchval("SELECT vector::text FROM embeddings WHERE id = $1", embedding_id)


@pytest.mark.asyncio
async def test_listing_finds_exactly_the_unusable_rows_and_honours_the_scope(pool):
    seeded = await _seed_legacy_document(pool)
    other = await _seed_legacy_document(pool)

    found = await list_unusable_embeddings([seeded["document_id"]])

    assert {item.embedding_id for item in found} == {seeded["recoverable"][1], seeded["hopeless"][1]}
    assert seeded["healthy"][1] not in {item.embedding_id for item in found}
    everything = {item.embedding_id for item in await list_unusable_embeddings()}
    assert {other["recoverable"][1], other["hopeless"][1]} <= everything  # unscoped sees all documents


@pytest.mark.asyncio
async def test_dry_run_reports_but_changes_nothing(pool):
    seeded = await _seed_legacy_document(pool)
    before = await _vector_text(pool, seeded["recoverable"][1])

    report = await reembed_unusable(dry_run=True, document_ids=[seeded["document_id"]])

    assert (report.scanned, report.repaired, report.removed, report.dry_run) == (2, 0, 0, True)
    assert await _vector_text(pool, seeded["recoverable"][1]) == before
    assert len(await list_unusable_embeddings([seeded["document_id"]])) == 2


@pytest.mark.asyncio
async def test_repair_reembeds_recoverable_chunks_and_removes_hopeless_ones(pool):
    seeded = await _seed_legacy_document(pool)
    healthy_before = await _vector_text(pool, seeded["healthy"][1])

    report = await reembed_unusable(document_ids=[seeded["document_id"]])

    assert (report.scanned, report.repaired, report.removed) == (2, 1, 1)
    # The Bengali chunk is searchable now: a usable vector, and a real match.
    self_distance = await pool.fetchval(
        "SELECT (vector <=> vector) FROM embeddings WHERE id = $1", seeded["recoverable"][1]
    )
    assert self_distance == 0.0  # NaN for a zero vector
    query = await HashingEmbeddingProvider().embed("শেয়ার বাজার")
    hits = await similarity_search(query, top_k=50)
    match = next(h for h in hits if h.chunk_id == seeded["recoverable"][0])
    assert match.similarity > 0.3
    # The hopeless chunk lost its embedding row (it can never match); its text remains.
    assert await pool.fetchval("SELECT count(*) FROM embeddings WHERE chunk_id = $1", seeded["hopeless"][0]) == 0
    assert await pool.fetchval("SELECT count(*) FROM knowledge_chunks WHERE id = $1", seeded["hopeless"][0]) == 1
    # The healthy chunk is untouched, and nothing is left to repair.
    assert await _vector_text(pool, seeded["healthy"][1]) == healthy_before
    assert await list_unusable_embeddings([seeded["document_id"]]) == []


@pytest.mark.asyncio
async def test_a_second_run_finds_nothing_to_do(pool):
    seeded = await _seed_legacy_document(pool)
    await reembed_unusable(document_ids=[seeded["document_id"]])

    again = await reembed_unusable(document_ids=[seeded["document_id"]])

    assert (again.scanned, again.repaired, again.removed) == (0, 0, 0)


@pytest.mark.asyncio
async def test_repair_is_confined_to_the_requested_documents(pool):
    target = await _seed_legacy_document(pool)
    bystander = await _seed_legacy_document(pool)

    await reembed_unusable(document_ids=[target["document_id"]])

    still_unusable = {item.embedding_id for item in await list_unusable_embeddings([bystander["document_id"]])}
    assert still_unusable == {bystander["recoverable"][1], bystander["hopeless"][1]}


# --------------------------------------------------------------------------
# The command line entry point (a real subprocess)
# --------------------------------------------------------------------------


def _cli_environment(api_root: Path, database_url: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "GOOGLE_AI_API_KEY")}
    env["PYTHONPATH"] = str(api_root)
    if database_url:
        env["DATABASE_URL"] = database_url
    return env


def _run_cli(*args: str, database_url: str | None, api_root: Path = API_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "app.rag.maintenance", *args],
        cwd=api_root,
        env=_cli_environment(api_root, database_url),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _isolated_api_copy(tmp_path: Path) -> tuple[Path, Path]:
    """
    A throwaway copy of the `app` package laid out like the repository
    (<root>/apps/api/app), returned as (root, api_dir).

    `config.load_env_files()` finds `.env.local` / `.env` from the module's
    own location — `apps/api` and the repo root — not from the working
    directory. The copy therefore sees only the env files written below,
    never the real `apps/api/.env.local`, while the code under test is the
    real package byte for byte (copied now, not a stub).

    The env files written here are decoys that configure NO database: they
    keep the loader doing real work (a file is found, parsed and applied)
    while guaranteeing it cannot supply DATABASE_URL.
    """
    root = tmp_path / "isolated-repo"
    api_dir = root / "apps" / "api"
    shutil.copytree(API_ROOT / "app", api_dir / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (api_dir / ".env.local").write_text("ISOLATED_TEST_DECOY=1\n")
    (root / ".env").write_text("ISOLATED_TEST_DECOY_ROOT=1\n")
    return root, api_dir


def _assert_isolated_from_any_database(root: Path, api_dir: Path) -> None:
    """
    Precondition, checked by actually running the loader in the isolated
    environment rather than assuming where it looks: after load_env_files()
    there must be no DATABASE_URL, and every file it applied must live
    inside the isolated tree. If `load_env_files()` ever grows a new search
    location, this fails here — with this message — instead of the test
    silently running against a real database.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; from app.config import load_env_files; "
            "applied = load_env_files(); "
            "print(json.dumps({'applied': applied, 'database_url_set': 'DATABASE_URL' in os.environ}))",
        ],
        cwd=api_dir,
        env=_cli_environment(api_dir, database_url=None),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    state = json.loads(probe.stdout)
    outside = [path for path in state["applied"] if not Path(path).resolve().is_relative_to(root.resolve())]
    assert not outside, f"the loader read env files outside the isolated tree: {outside}"
    assert len(state["applied"]) == 2, f"expected the two decoy env files to be loaded, got {state['applied']}"
    assert state["database_url_set"] is False, (
        f"the isolated environment still configures a database (env files applied: {state['applied']})"
    )


def test_command_line_dry_run_and_repair_round_trip():
    database_url = os.environ["DATABASE_URL"]

    async def seed() -> dict:
        connection = await asyncpg.connect(database_url)
        try:
            return await _seed_legacy_document(connection)  # a Connection has fetchval/execute like a Pool
        finally:
            await connection.close()

    async def count_unusable(document_id: str) -> int:
        connection = await asyncpg.connect(database_url)
        try:
            return await connection.fetchval(
                "SELECT count(*) FROM embeddings e JOIN knowledge_chunks kc ON kc.id = e.chunk_id "
                "WHERE kc.document_id = $1 AND (e.vector <=> e.vector) = 'NaN'::float8",
                document_id,
            )
        finally:
            await connection.close()

    async def cleanup() -> None:
        connection = await asyncpg.connect(database_url)
        try:
            await delete_test_documents(connection)
        finally:
            await connection.close()

    seeded = asyncio.run(seed())
    try:
        document = seeded["document_id"]

        dry = _run_cli("reembed-unusable", "--dry-run", "--document-id", document, database_url=database_url)
        assert dry.returncode == 0, dry.stderr
        assert "2 unusable embedding(s) found" in dry.stdout and "dry run" in dry.stdout
        assert asyncio.run(count_unusable(document)) == 2

        real = _run_cli("reembed-unusable", "--document-id", document, database_url=database_url)
        assert real.returncode == 0, real.stderr
        assert "1 were re-embedded, 1 were removed" in real.stdout
        assert asyncio.run(count_unusable(document)) == 0
    finally:
        asyncio.run(cleanup())


def test_command_line_without_a_database_exits_with_a_clear_message(tmp_path):
    root, api_dir = _isolated_api_copy(tmp_path)
    _assert_isolated_from_any_database(root, api_dir)

    # --dry-run is defence in depth: were isolation ever to fail, this could
    # only report, never modify a real database.
    result = _run_cli("reembed-unusable", "--dry-run", database_url=None, api_root=api_dir)

    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "DATABASE_URL is not set" in result.stderr
    assert result.stdout == ""  # nothing was found, reported or changed