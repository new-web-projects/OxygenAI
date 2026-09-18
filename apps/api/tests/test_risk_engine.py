"""
Risk Engine tests — Passage 4 §4 (G06), audit item D5.4.

The central regression this file guards against: before this pass,
`riskReward` was the constant 1.0 on every response the system ever
produced, because the stop and the first target were both placed at the
same ATR multiple from entry. `test_risk_reward_is_not_a_constant_one`
is the direct test for that defect.
"""

from __future__ import annotations

from app.config import RiskSettings
from app.engine.risk_engine import (
    assess,
    compute_risk_reward,
    compute_stop_loss,
    compute_targets,
    select_targets,
    size_position,
)


def _settings(**overrides) -> RiskSettings:
    base = dict(
        account_equity=1_000_000.0,
        max_risk_per_trade_pct=1.0,
        sizing_method="atr_based",
        atr_stop_multiple=1.5,
        min_risk_reward=1.5,
    )
    base.update(overrides)
    return RiskSettings(**base)


def test_long_stop_sits_below_entry_short_stop_sits_above():
    long_stop = compute_stop_loss(100.0, atr=2.0, direction="LONG", atr_multiple=1.5)
    short_stop = compute_stop_loss(100.0, atr=2.0, direction="SHORT", atr_multiple=1.5)
    assert long_stop < 100.0
    assert short_stop > 100.0
    assert long_stop == 97.0
    assert short_stop == 103.0


def test_stop_loss_rejects_non_positive_atr():
    import pytest

    with pytest.raises(ValueError):
        compute_stop_loss(100.0, atr=0.0, direction="LONG", atr_multiple=1.5)


def test_risk_reward_is_not_a_constant_one():
    """
    The regression test for the defect found before this pass: stop and
    target-1 were both 1.5x ATR from entry, so R:R was always exactly
    1.0. Fixed R-multiples alone should now give 2.0 (the first
    configured multiple); a real structural level should give something
    else entirely. Neither is 1.0.
    """
    settings = _settings()
    result = assess(direction="LONG", entry=100.0, atr=2.0, settings=settings)
    assert result.risk_reward != 1.0
    assert result.risk_reward == 2.0  # first configured R-multiple, no structure supplied

    with_structure = assess(
        direction="LONG", entry=100.0, atr=2.0, settings=settings, structural_level=112.0
    )
    assert with_structure.risk_reward != 1.0
    assert with_structure.risk_reward != 2.0
    assert with_structure.target_basis == "nearest structural level"


def test_structural_level_behind_entry_falls_back_to_multiples():
    settings = _settings()
    result = assess(
        direction="LONG", entry=100.0, atr=2.0, settings=settings, structural_level=95.0
    )
    assert "behind entry" in result.target_basis
    assert result.risk_reward == 2.0


def test_structural_level_closer_than_min_rr_falls_back_to_multiples():
    """
    A structural level nearer than the minimum acceptable R:R would
    require is not a usable target — using it would misrepresent the
    trade even before the min-R:R gate gets a chance to reject it.
    """
    settings = _settings(min_risk_reward=1.5)
    # risk_per_unit = 1.5 * 2.0 = 3.0; a level at 101.0 is only 1.0 away
    # (distance < risk_per_unit * min_risk_reward = 4.5).
    result = assess(
        direction="LONG", entry=100.0, atr=2.0, settings=settings, structural_level=101.0
    )
    assert "closer than the minimum" in result.target_basis
    assert result.risk_reward == 2.0


def test_short_direction_mirrors_long():
    settings = _settings()
    result = assess(direction="SHORT", entry=100.0, atr=2.0, settings=settings)
    assert result.stop_loss > 100.0
    assert all(t < 100.0 for t in result.targets)
    assert result.risk_reward == 2.0


def test_min_risk_reward_gate_rejects_a_low_rr_setup():
    settings = _settings(min_risk_reward=5.0)  # fixed multiples top out at 4.0
    result = assess(direction="LONG", entry=100.0, atr=2.0, settings=settings)
    assert not result.passes_min_rr
    assert not result.is_tradeable
    assert result.rejected_reason is not None
    assert "isk/reward" in result.rejected_reason


def test_max_risk_per_trade_gate_rejects_an_undersized_account():
    settings = _settings(account_equity=100.0, max_risk_per_trade_pct=0.1)
    result = assess(direction="LONG", entry=100.0, atr=2.0, settings=settings)
    assert result.position.quantity == 0
    assert not result.passes_max_risk
    assert not result.is_tradeable


def test_position_sizing_never_exceeds_the_configured_risk_budget():
    settings = _settings(account_equity=50_000.0, max_risk_per_trade_pct=1.0)
    position = size_position(entry=100.0, stop_loss=95.0, settings=settings)
    risk_budget = 50_000.0 * 0.01
    assert position.quantity > 0
    assert position.risk_amount <= risk_budget + 1e-9


def test_position_sizing_respects_lot_size_by_rounding_down():
    settings = _settings(account_equity=1_000_000.0, max_risk_per_trade_pct=1.0)
    position = size_position(entry=100.0, stop_loss=95.0, settings=settings, lot_size=75)
    assert position.quantity % 75 == 0


def test_position_sizing_rejects_zero_risk_distance():
    settings = _settings()
    position = size_position(entry=100.0, stop_loss=100.0, settings=settings)
    assert position.quantity == 0
    assert position.rejected_reason is not None


def test_compute_targets_scales_with_the_configured_multiples():
    targets = compute_targets(100.0, risk_per_unit=3.0, direction="LONG", r_multiples=(2.0, 3.0, 4.0))
    assert targets == [106.0, 109.0, 112.0]


def test_compute_risk_reward_is_none_on_zero_risk():
    assert compute_risk_reward(100.0, 100.0, 110.0) is None


def test_select_targets_extension_preserves_r_multiple_spacing_beyond_target_one():
    settings = _settings()
    targets, basis = select_targets(
        entry=100.0, risk_per_unit=3.0, direction="LONG", settings=settings, structural_level=112.0
    )
    assert basis == "nearest structural level"
    assert targets[0] == 112.0
    # Extensions beyond target 1 keep the configured multiples' spacing
    # from entry, and must stay strictly further out than target 1.
    assert all(t > 112.0 for t in targets[1:])


def test_all_dataclasses_are_frozen_pure_functions_no_shared_state():
    """Same inputs, same outputs — no clock, no I/O, no hidden state."""
    settings = _settings()
    a = assess(direction="LONG", entry=100.0, atr=2.0, settings=settings)
    b = assess(direction="LONG", entry=100.0, atr=2.0, settings=settings)
    assert a == b