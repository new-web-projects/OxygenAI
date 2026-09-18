"""
Model registry tests — Passage 4 §3.2 (G02): retired-slug redirects,
the URL-only spelling correction, and the coding-model exclusion.
"""

from __future__ import annotations

from app.providers.model_registry import ModelRegistry


def test_retired_slugs_redirect_to_the_current_flagship():
    registry = ModelRegistry()
    for retired in ("grok-4-1-fast", "grok-4-fast", "grok-4-0709", "grok-3"):
        effective, note = registry.resolve_model_slug("grok", retired)
        assert effective == "grok-4.3"
        assert note is not None
        assert "retired" in note.lower()


def test_url_only_spelling_is_corrected_to_the_api_value():
    registry = ModelRegistry()
    effective, note = registry.resolve_model_slug("grok", "grok-4-6")
    assert effective == "grok-4.6"
    assert note is not None


def test_current_flagship_passes_through_unchanged():
    registry = ModelRegistry()
    effective, note = registry.resolve_model_slug("grok", "grok-4.6")
    assert effective == "grok-4.6"
    assert note is None


def test_coding_model_is_never_used_for_trading_analysis():
    registry = ModelRegistry()
    effective, note = registry.resolve_model_slug("grok", "grok-code-fast-1")
    assert effective != "grok-code-fast-1"
    assert effective != "grok-build-0.1"  # its successor is also non-routable
    assert note is not None


def test_unknown_slug_passes_through_untouched():
    """
    An unrecognised slug must not be blocked — the registry is a known
    catalogue, not an allowlist, and a brand-new model shouldn't be
    rejected just because this snapshot predates it.
    """
    registry = ModelRegistry()
    effective, note = registry.resolve_model_slug("grok", "grok-9-hypothetical")
    assert effective == "grok-9-hypothetical"
    assert note is None


def test_gemma_models_are_never_labelled_gemini():
    """
    Passage 1 §4.3.1's hard identity-vs-transport rule, checked
    directly against the registry's own data rather than trusted by
    convention.
    """
    registry = ModelRegistry()
    for entry in registry.all("gemma"):
        assert "gemini" not in entry.display_name.lower()
        assert "gemini" not in entry.model_id.lower()


def test_default_model_id_is_marked_is_default_in_the_catalogue():
    registry = ModelRegistry()
    default_id = registry.default_model_id("grok")
    assert default_id == "grok-4.6"
    entry = registry.get("grok", default_id)
    assert entry is not None
    assert entry.is_default


def test_mark_model_retired_updates_status_and_sets_a_redirect():
    registry = ModelRegistry()
    registry.mark_model_retired("grok", "grok-4.6", "xAI reported 404 for this slug")
    entry = registry.get("grok", "grok-4.6")
    assert entry is not None
    assert entry.status == "retired"
    # A fallback model exists for grok, so the newly-retired entry should
    # point somewhere else the router can actually use.
    assert entry.redirects_to is not None
    assert entry.redirects_to != "grok-4.6"