"""
Tokenizer and hashing-embedder regression tests (no database).

Root cause of the knowledge-search failure: the embedder tokenized with
`[a-z0-9]+`, so text with no ASCII letter/digit — Bengali, Hindi, Arabic,
CJK, punctuation-only — produced no tokens, hence an all-zero vector, hence
NaN similarity in pgvector, hence an un-serialisable response. These tests
pin (1) that those scripts now embed to a usable vector, and (2) that
nothing changed for ASCII text, so every vector stored by earlier versions
stays valid.
"""

from __future__ import annotations

import hashlib
import math
import random
import re

import pytest

from app.db.knowledge import is_usable_vector
from app.rag.embeddings import EMBEDDING_DIM, HashingEmbeddingProvider
from app.rag.tokenize import tokenize


# --- Frozen copy of the PREVIOUS implementation: the compatibility reference.
# Deliberately duplicated here (not imported) so that if the production
# algorithm ever drifts, this test notices.
def _legacy_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _legacy_embed(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    words = _legacy_tokens(text)
    if not words:
        return vector
    for word in words:
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        vector[bucket] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


ASCII_SAMPLES = [
    "RSI measures momentum. A reading above 70 is overbought.",
    "Position sizing: risk 1% of equity per trade; ATR-based stops.",
    "UPPER lower MiXeD case, snake_case_words and kebab-case-words",
    "don't, can't, won't — it's 100% about 2.5R targets & 3 R:R.",
    "NIFTY50 / BANKNIFTY 22,450.75 (+1.2%)",
    "",
    "   \t\n  ",
    "!!! ??? ---",
]


def _random_ascii(rng: random.Random) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \t\n.,;:!?'\"-_()[]{}<>/\\@#$%^&*+=~`|"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))


# ---------------------------------------------------------------- compatibility


def test_ascii_tokenization_is_identical_to_the_legacy_regex():
    rng = random.Random(20261016)
    corpus = ASCII_SAMPLES + [_random_ascii(rng) for _ in range(500)]
    for text in corpus:
        assert tokenize(text) == _legacy_tokens(text), repr(text)


@pytest.mark.asyncio
async def test_ascii_embeddings_are_bit_identical_to_the_legacy_algorithm():
    # Exact equality, not approximate: previously stored vectors must remain
    # valid for similarity against freshly computed query vectors.
    provider = HashingEmbeddingProvider()
    rng = random.Random(7)
    for text in ASCII_SAMPLES + [_random_ascii(rng) for _ in range(60)]:
        assert await provider.embed(text) == _legacy_embed(text), repr(text)


def test_model_name_is_unchanged_so_existing_rows_stay_valid():
    assert HashingEmbeddingProvider.model_name == "hashing-trick-v1"


# ---------------------------------------------------------------- the actual bug


NON_ASCII_SAMPLES = {
    "bengali": ("ভারতীয় শেয়ার বাজার বিশ্লেষণ", ["ভারতীয়", "শেয়ার", "বাজার", "বিশ্লেষণ"]),
    "hindi + english": ("RSI और MACD का उपयोग", ["rsi", "और", "macd", "का", "उपयोग"]),
    "cyrillic": ("Индекс Относительной Силы", ["индекс", "относительной", "силы"]),
    "greek": ("Μέση τιμή", ["μέση", "τιμή"]),
    "arabic": ("مؤشر القوة النسبية", ["مؤشر", "القوة", "النسبية"]),
    "accented latin": ("Café naïve", ["café", "naïve"]),
    "fullwidth latin": ("ＲＳＩ ７０", ["rsi", "70"]),
    "ligature": ("ﬁnance", ["finance"]),
}


@pytest.mark.parametrize("name", list(NON_ASCII_SAMPLES))
def test_non_ascii_text_tokenizes_into_whole_words(name):
    text, expected = NON_ASCII_SAMPLES[name]
    assert tokenize(text) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(NON_ASCII_SAMPLES))
async def test_non_ascii_text_embeds_to_a_usable_vector(name):
    # Before the fix every sample here except 'hindi + english' and the
    # Latin-accent cases came back as the all-zero vector.
    text, _ = NON_ASCII_SAMPLES[name]
    vector = await HashingEmbeddingProvider().embed(text)
    assert len(vector) == EMBEDDING_DIM
    assert is_usable_vector(vector)
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


@pytest.mark.asyncio
async def test_a_bengali_query_is_most_similar_to_a_bengali_document():
    provider = HashingEmbeddingProvider()
    doc_bn = await provider.embed("ভারতীয় শেয়ার বাজার বিশ্লেষণ এবং ঝুঁকি ব্যবস্থাপনা")
    doc_en = await provider.embed("RSI measures momentum and overbought conditions")
    query = await provider.embed("শেয়ার বাজার")

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b))  # both unit length

    assert cosine(query, doc_bn) > 0.3
    assert cosine(query, doc_en) == 0.0


def test_combining_marks_stay_attached_so_indic_words_are_not_shredded():
    # "বাজার" contains vowel signs (category Mc). Splitting on "non-letters"
    # would have produced single consonants.
    assert tokenize("বাজার") == ["বাজার"]


def test_composed_and_decomposed_forms_tokenize_identically():
    assert tokenize("caf\u00e9") == tokenize("cafe\u0301") == ["café"]


def test_a_joiner_keeps_an_indic_conjunct_in_one_token():
    tokens = tokenize("ক্\u200dষ")
    assert len(tokens) == 1 and "\u200d" in tokens[0]


# ---------------------------------------------------------------- genuinely empty


CONTENTLESS = ["", "   \n\t", "???", "--- !!! ...", "😀😀", "\u0301\u0301", "\u200d", "\u200c\u200d"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", CONTENTLESS)
async def test_contentless_text_has_no_tokens_and_no_usable_vector(text):
    assert tokenize(text) == []
    vector = await HashingEmbeddingProvider().embed(text)
    assert all(v == 0.0 for v in vector)  # an honest "no signal" ...
    assert not is_usable_vector(vector)  # ... that callers must not store or search with


def test_circled_digits_normalise_to_real_digits_so_they_are_not_contentless():
    assert tokenize("①②③") == ["123"]


def test_tokenizer_never_raises_on_malformed_unicode():
    # A lone surrogate can arrive in a JSON body ("\ud800"); it must be a
    # separator, not a crash (and never end up inside a hashed token).
    assert tokenize("a\ud800b") == ["a", "b"]


@pytest.mark.asyncio
async def test_lone_surrogate_does_not_break_embedding():
    assert is_usable_vector(await HashingEmbeddingProvider().embed("rsi\ud800momentum"))


@pytest.mark.asyncio
async def test_exact_bucket_cancellation_yields_the_zero_vector_not_a_crash():
    """Two different words can land in the same bucket with opposite signs,
    summing to exactly zero. Found deterministically here rather than assumed."""
    provider = HashingEmbeddingProvider()
    seen: dict[int, tuple[str, float]] = {}
    pair = None
    for i in range(20000):
        word = f"w{i}"
        bucket, sign = provider._hash_bucket(word)
        if bucket in seen and seen[bucket][1] != sign:
            pair = (seen[bucket][0], word)
            break
        seen.setdefault(bucket, (word, sign))
    assert pair is not None, "no colliding pair found"
    vector = await provider.embed(" ".join(pair))
    assert all(v == 0.0 for v in vector)
    assert not is_usable_vector(vector)
