"""
Knowledge-base maintenance — repair for the zero-vector defect.

    python -m app.rag.maintenance reembed-unusable [--dry-run] [--document-id UUID ...]

Background: an earlier version stored an all-zero embedding for any chunk
with no ASCII letters or digits (a Bengali or Hindi document, for
example). Cosine similarity against a zero vector is NaN, which is what made
`POST /api/knowledge/search` fail. New code can no longer write such rows
and search now skips any that remain, but a skipped row is also an
UNSEARCHABLE row. This command finds them and re-embeds their text with the
current embedder:

`--document-id` (repeatable) limits the repair to those documents.

* the chunk now yields a usable vector (e.g. Bengali text, now that
  tokenization is Unicode-aware) -> the stored vector is replaced and the
  chunk becomes searchable;
* the chunk still has nothing embeddable (only punctuation or symbols) ->
  its embedding row is deleted, since it can never match anything.

The change is one transaction per batch, safe to re-run (the second run
finds nothing), and `--dry-run` changes nothing. It uses whichever embedder
is configured now (`GOOGLE_AI_API_KEY` set or not) and records that in
`embeddings.model_used`. It does NOT re-embed healthy rows — switching
embedder for the whole corpus is a separate, larger operation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import load_env_files
from ..db.client import close_pool, is_db_configured
from ..db.knowledge import apply_embedding_repairs, is_usable_vector, list_unusable_embeddings
from ..observability import get_logger
from .embeddings import get_embedding_provider

logger = get_logger("rag.maintenance")

DEFAULT_BATCH_SIZE = 64


@dataclass(frozen=True)
class RepairReport:
    scanned: int  # unusable embeddings found
    repaired: int  # re-embedded into a usable vector
    removed: int  # deleted: the chunk has nothing embeddable
    dry_run: bool


async def reembed_unusable(
    *,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    document_ids: Sequence[str] | None = None,
) -> RepairReport:
    found = await list_unusable_embeddings(document_ids)
    if dry_run or not found:
        return RepairReport(scanned=len(found), repaired=0, removed=0, dry_run=dry_run)

    provider = get_embedding_provider()
    repaired = removed = 0
    for start in range(0, len(found), batch_size):
        batch = found[start : start + batch_size]
        vectors = await provider.embed_batch([item.content for item in batch])
        if len(vectors) != len(batch):
            raise ValueError(f"Embedding provider returned {len(vectors)} vectors for {len(batch)} chunks")
        replacements = [(i.embedding_id, v) for i, v in zip(batch, vectors) if is_usable_vector(v)]
        deletions = [i.embedding_id for i, v in zip(batch, vectors) if not is_usable_vector(v)]
        await apply_embedding_repairs(replacements, deletions, provider.model_name)
        repaired += len(replacements)
        removed += len(deletions)
        logger.info(
            "re-embedded unusable embeddings",
            extra={"repaired": len(replacements), "removed": len(deletions), "model": provider.model_name},
        )
    return RepairReport(scanned=len(found), repaired=repaired, removed=removed, dry_run=False)


async def _run(dry_run: bool, document_ids: Sequence[str] | None) -> RepairReport:
    try:
        return await reembed_unusable(dry_run=dry_run, document_ids=document_ids)
    finally:
        await close_pool()


def main(argv: list[str] | None = None) -> int:
    load_env_files()
    parser = argparse.ArgumentParser(prog="python -m app.rag.maintenance", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    repair = sub.add_parser("reembed-unusable", help="Re-embed (or drop) zero-vector embeddings")
    repair.add_argument("--dry-run", action="store_true", help="Report what would change; change nothing")
    repair.add_argument(
        "--document-id", action="append", metavar="UUID", help="Only repair this document (repeatable)"
    )
    args = parser.parse_args(argv)

    if not is_db_configured():
        print("DATABASE_URL is not set — nothing to do.", file=sys.stderr)
        return 2

    report = asyncio.run(_run(args.dry_run, args.document_id))
    if report.dry_run:
        # A dry run does not compute embeddings, so it cannot know how many
        # rows would be repaired vs dropped — it reports only what it found.
        print(f"{report.scanned} unusable embedding(s) found.  [dry run: no changes made]")
    else:
        print(
            f"{report.scanned} unusable embedding(s) found. "
            f"{report.repaired} were re-embedded, {report.removed} were removed (nothing embeddable)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
