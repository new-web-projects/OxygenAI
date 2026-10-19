"""
Shared tokenizer for the RAG layer (embedding + lexical rerank).

Why this exists — the root cause of the "knowledge search returns HTTP
500 / ValueError: Out of range float values are not JSON compliant: nan"
failure:

The hashing embedder and the reranker each tokenized with `[a-z0-9]+`, so
any text containing no ASCII letter or digit — a Bengali or Hindi
document, an Arabic or CJK note, a chunk of pure punctuation — produced
ZERO tokens. The embedder answered with the all-zero vector, which was
stored as-is. pgvector's cosine distance against a zero vector is NaN
(0/0), so `1 - distance` made the similarity score NaN, and FastAPI's JSON
encoder refuses NaN. Every search that touched such a chunk (or used such
a query) failed, while uploads of the same content "succeeded".

This tokenizer is Unicode-aware, so those scripts yield real tokens and a
real, non-zero embedding. Compatibility is deliberate and tested:

* Pure-ASCII text takes the SAME regex path as before, so every token —
  and therefore every previously stored hashing vector and its similarity
  to new queries — is bit-for-bit unchanged.
* Non-ASCII text is NFKC-normalised and case-folded, then split on any
  character that is not a letter, number, or combining mark. Keeping
  combining marks (category M) matters: Indic vowel signs and viragas are
  marks, so splitting on "non-letters" would shred "বাজার" into single
  consonants. Zero-width (non-)joiners stay inside words (they occur
  within Indic and Persian words) but never form a token on their own.

Known limitation, stated rather than hidden: scripts written without
spaces (Chinese, Japanese, Thai) become one token per run of text, so
lexical similarity only fires on identical runs. They no longer fail, but
they are not well served by a hashing-trick embedding; a real embedding
model (`GOOGLE_AI_API_KEY`) is the right tool for them.
"""

from __future__ import annotations

import re
import unicodedata

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")

# Letters, numbers and combining marks make up words.
_WORD_CATEGORIES = frozenset("LNM")
# Joiners keep a word together (Indic conjuncts, Persian compounds) but are
# not words themselves.
_JOINERS = frozenset("\u200c\u200d")


def _is_word_char(ch: str) -> bool:
    return ch in _JOINERS or unicodedata.category(ch)[0] in _WORD_CATEGORIES


def _has_substance(token: str) -> bool:
    """A token needs at least one letter or digit; a lone mark or joiner is noise."""
    return any(unicodedata.category(ch)[0] in "LN" for ch in token)


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens of `text`, in order. Never raises."""
    if text.isascii():
        return _ASCII_WORD_RE.findall(text.lower())

    tokens: list[str] = []
    current: list[str] = []
    for ch in unicodedata.normalize("NFKC", text).casefold():
        if _is_word_char(ch):
            current.append(ch)
        elif current:
            token = "".join(current)
            if _has_substance(token):
                tokens.append(token)
            current.clear()
    if current:
        token = "".join(current)
        if _has_substance(token):
            tokens.append(token)
    return tokens
