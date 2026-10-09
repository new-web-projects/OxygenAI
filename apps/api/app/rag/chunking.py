"""
Text extraction, cleaning, and chunking — Passage 1 §7's first three
pipeline stages: "UPLOAD (PDF/TXT/CSV) -> TEXT EXTRACTION -> CLEANING ->
CHUNKING (semantic, ~500-800 tokens, 100-token overlap)".

Token counts are approximated by word count (~0.75 tokens/word, the
commonly-cited average for English), since no tokenizer that matches
any of this project's three real providers (Custom AI's configured
model, Grok, Gemma 4) is bundled here — pulling in a mismatched one
(e.g. OpenAI's tiktoken) would give a false sense of precision rather
than genuine accuracy for any of them. Documented rather than hidden:
this is why the target below is expressed in words (375-600, 75-word
overlap) with the token figures kept alongside as the stated intent.

"Semantic" chunking here means sentence-boundary-aware: chunks are
packed to the target size but never split mid-sentence when a sentence
boundary is available nearby, which keeps each chunk a coherent unit of
meaning rather than an arbitrary word-count slice.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

# ~500-800 tokens at ~0.75 tokens/word -> ~375-600 words; 100 tokens -> ~75 words.
TARGET_MIN_WORDS = 375
TARGET_MAX_WORDS = 600
OVERLAP_WORDS = 75

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class UnsupportedFileTypeError(ValueError):
    pass


def extract_text(raw: bytes, content_type: str, filename: str = "") -> str:
    """
    Passage 1 §7's "TEXT EXTRACTION" stage, for the three formats its
    own upload diagram names: PDF, TXT, CSV.
    """
    lower_name = filename.lower()
    if content_type == "application/pdf" or lower_name.endswith(".pdf"):
        return _extract_pdf(raw)
    if content_type in ("text/csv", "application/csv") or lower_name.endswith(".csv"):
        return _extract_csv(raw)
    # Plain text is the fallback for text/plain and anything else that
    # decodes as UTF-8 — refusing an unlabelled .txt upload over a
    # missing/generic content-type header would be unhelpfully strict.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise UnsupportedFileTypeError(
            f"Could not decode '{filename}' as PDF, CSV, or UTF-8 text."
        ) from err


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def _extract_csv(raw: bytes) -> str:
    """
    Flattens rows to "col: value" lines rather than a raw comma-joined
    dump — this is what makes a chunk of a CSV row readable as prose
    once it's retrieved and dropped into a provider's prompt, instead of
    an opaque list of bare values with no column context.
    """
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    lines: list[str] = []
    for row in reader:
        lines.append("; ".join(f"{k}: {v}" for k, v in row.items() if k))
    return "\n".join(lines)


def clean_text(text: str) -> str:
    """Passage 1 §7's "CLEANING" stage: collapse whitespace, drop excess blank lines."""
    text = text.replace("\r\n", "\n").replace("\x00", "")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class Chunk:
    content: str
    position: int


def _sentence_tail_by_words(text: str, max_words: int) -> list[str]:
    """The trailing sentences of `text` whose combined word count is <= max_words."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    tail: list[str] = []
    count = 0
    for sentence in reversed(sentences):
        words = len(sentence.split())
        if count + words > max_words:
            break
        tail.insert(0, sentence)
        count += words
    return tail


def chunk_text(text: str) -> list[Chunk]:
    """
    Sentence-boundary-aware chunking with overlap. Greedily packs
    sentences into a chunk until adding the next one would exceed
    `TARGET_MAX_WORDS`, then closes it and seeds the next chunk with the
    trailing `OVERLAP_WORDS` worth of sentences from the one just
    closed — so a fact sitting near a chunk boundary isn't invisible to
    whichever side of the boundary it doesn't happen to fall on.
    """
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return []

    chunks: list[Chunk] = []
    current: list[str] = []
    current_words = 0
    position = 0

    def flush() -> None:
        nonlocal current, current_words, position
        if not current:
            return
        chunks.append(Chunk(content=" ".join(current).strip(), position=position))
        position += 1

    for sentence in sentences:
        sentence_words = len(sentence.split())
        if current and current_words + sentence_words > TARGET_MAX_WORDS:
            flush()
            overlap = _sentence_tail_by_words(chunks[-1].content, OVERLAP_WORDS)
            current = list(overlap)
            current_words = sum(len(s.split()) for s in overlap)
        current.append(sentence)
        current_words += sentence_words

    flush()
    return chunks