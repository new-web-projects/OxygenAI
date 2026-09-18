"""
Normalisation tests — the regression suite for the defect confirmed
before this pass: a provider replying `"long"` (lowercase) or `"BUY"`
raised a Pydantic ValidationError deep inside `build_trade_analysis`,
surfacing as a misleading 503 in single mode and a raw traceback string
in a comparison slot's `reason` field.
"""

from __future__ import annotations

from app.providers.normalize import (
    extract_json_object,
    normalize_confidence,
    normalize_direction,
    normalize_evidence,
    parse_reasoning_payload,
)


class TestNormalizeDirection:
    def test_exact_long_and_short_pass_through(self):
        assert normalize_direction("LONG") == "LONG"
        assert normalize_direction("SHORT") == "SHORT"

    def test_lowercase_and_mixed_case_are_recognised(self):
        # The exact regression: this used to reach Pydantic unmodified
        # and raise ValidationError.
        assert normalize_direction("long") == "LONG"
        assert normalize_direction("Short") == "SHORT"

    def test_common_synonyms_are_recognised(self):
        assert normalize_direction("BUY") == "LONG"
        assert normalize_direction("sell") == "SHORT"
        assert normalize_direction("bullish") == "LONG"
        assert normalize_direction("bearish") == "SHORT"

    def test_neutral_language_maps_to_no_setup(self):
        assert normalize_direction("neutral") is None
        assert normalize_direction("hold") is None
        assert normalize_direction(None) is None
        assert normalize_direction("") is None

    def test_unrecognised_value_becomes_none_never_a_guess(self):
        assert normalize_direction("maybe up a bit") is None
        assert normalize_direction(12345) is None

    def test_booleans_are_never_mistaken_for_a_direction(self):
        assert normalize_direction(True) is None
        assert normalize_direction(False) is None


class TestNormalizeConfidence:
    def test_zero_to_one_fraction_is_scaled_to_percentage(self):
        assert normalize_confidence(0.85) == 85.0
        assert normalize_confidence(0.5) == 50.0

    def test_percentage_scale_passes_through(self):
        assert normalize_confidence(70) == 70.0
        assert normalize_confidence(85.5) == 85.5

    def test_edge_value_one_is_read_as_the_top_of_the_fractional_range(self):
        # 1 is treated the same as the rest of [0, 1] — as a fraction —
        # rather than carved out as a special "1%" case. See the
        # docstring in normalize.py for why the uniform reading won out.
        assert normalize_confidence(1) == 100.0

    def test_out_of_range_values_are_clamped_not_rejected(self):
        assert normalize_confidence(150) == 100.0
        assert normalize_confidence(-10) == 0.0

    def test_string_percentage_is_parsed(self):
        assert normalize_confidence("70%") == 70.0
        assert normalize_confidence("0.6") == 60.0

    def test_none_and_garbage_become_none(self):
        assert normalize_confidence(None) is None
        assert normalize_confidence("not a number") is None
        assert normalize_confidence(float("nan")) is None
        assert normalize_confidence(float("inf")) is None


class TestNormalizeEvidence:
    def test_accepts_a_list(self):
        assert normalize_evidence(["a", "b"]) == ["a", "b"]

    def test_splits_a_newline_delimited_string(self):
        result = normalize_evidence("rsi14 is high\n- macd crossed\n")
        assert result == ["rsi14 is high", "macd crossed"]

    def test_none_becomes_an_empty_list(self):
        assert normalize_evidence(None) == []


class TestExtractJsonObject:
    def test_plain_json_parses_directly(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_strips_markdown_fences(self):
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_finds_the_first_balanced_object_in_surrounding_prose(self):
        text = 'Here is my answer: {"a": 1, "b": {"c": 2}} — hope that helps!'
        assert extract_json_object(text) == {"a": 1, "b": {"c": 2}}

    def test_does_not_greedily_span_two_separate_objects(self):
        """
        The regression this guards: a naive greedy regex from '{' to the
        *last* '}' would merge two separate JSON-looking blocks into one
        invalid string. The brace-counting scan must return only the
        first genuinely balanced object.
        """
        text = 'First {"a": 1} then unrelated {"b": 2}'
        result = extract_json_object(text)
        assert result == {"a": 1}

    def test_braces_inside_string_literals_do_not_confuse_the_scanner(self):
        text = '{"note": "a { b } c", "value": 5}'
        assert extract_json_object(text) == {"note": "a { b } c", "value": 5}

    def test_raises_on_empty_input(self):
        import pytest

        with pytest.raises(ValueError):
            extract_json_object("")

    def test_raises_when_no_object_is_present(self):
        import pytest

        with pytest.raises(ValueError):
            extract_json_object("just some prose, no JSON here")


class TestParseReasoningPayload:
    def test_full_regression_case_lowercase_direction_and_fraction_confidence(self):
        """The exact combination that used to crash: 'long' + a 0-1 confidence."""
        payload = '{"direction": "long", "confidence": 0.72, "reasoningSummary": "trend is up"}'
        parsed = parse_reasoning_payload(payload)
        assert parsed["direction"] == "LONG"
        assert parsed["confidence"] == 72.0
        assert parsed["reasoning_summary"] == "trend is up"

    def test_field_name_lookup_is_case_and_separator_insensitive(self):
        payload = '{"Direction": "SHORT", "Confidence_Score": 60, "reasoning": "x"}'
        parsed = parse_reasoning_payload(payload)
        assert parsed["direction"] == "SHORT"
        assert parsed["confidence"] == 60.0

    def test_missing_summary_gets_an_honest_fallback_not_a_crash(self):
        parsed = parse_reasoning_payload('{"direction": "LONG"}')
        assert "no reasoning summary" in parsed["reasoning_summary"].lower()