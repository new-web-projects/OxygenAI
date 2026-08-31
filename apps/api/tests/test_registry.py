from app.providers.registry import list_providers, resolve_provider


def test_resolve_provider_falls_back_to_mock_for_unknown_id():
    assert resolve_provider("does-not-exist").id == "mock"


def test_resolve_provider_falls_back_to_mock_for_gemma_when_unconfigured():
    assert resolve_provider("gemma").id == "mock"  # no GOOGLE_AI_API_KEY set here


def test_resolve_provider_falls_back_to_mock_for_grok_when_unconfigured():
    assert resolve_provider("grok").id == "mock"  # no XAI_API_KEY set here


def test_resolve_provider_falls_back_to_mock_for_custom_when_unconfigured():
    assert resolve_provider("custom").id == "mock"  # no CUSTOM_AI_* set here


def test_registry_is_exactly_mock_custom_grok_gemma_no_gemini_entry():
    ids = sorted(p["id"] for p in list_providers())
    assert ids == ["custom", "gemma", "grok", "mock"]
