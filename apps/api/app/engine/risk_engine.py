"""
Risk Engine — Passage 1 §6, recovered in detail by Passage 4 §4 (G06):
"position sizing (fixed-fractional or ATR-based), max-risk-per-trade
enforcement, R:R computation — pure functions, unit-tested independently
of any AI call, from any provider."

This was the single most consequential gap in the audit (D5.4). Before
this pass, only R:R existed, inlined in `verify.py`, and it was
structurally broken: stop-loss was set at 1.5 x ATR below entry and
target-1 at 1.5 x ATR above it, so risk and reward were equal by
construction and `riskReward` was the constant 1.0 on every response
ever produced. That made the "risk/reward quality" comparison axis a
constant 50 as well. Verified across ten symbols before rewriting:
every single one returned exactly (1.0, 50.0).

Fixed here by separating the two multiples that were wrongly identical:
the stop distance is a volatility measure (ATR-based), while targets are
expressed as R-multiples of that distance. Targets at 2R/3R now produce
a genuine, varying R:R that clears the configured floor.

Every function in this module is pure: same inputs, same outputs, no
clock, no I/O, no provider call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import RiskSettings

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class PositionSize:
    """Result of position sizing. `quantity` of 0 means 'do not take this trade'."""

    quantity: int
    notional: float
    risk_amount: float
    risk_pct_of_equity: float
    method: str
    rejected_reason: str | None = None


@dataclass(frozen=True)
class RiskAssessment:
    direction: Direction
    entry: float
    stop_loss: float
    targets: list[float]
    risk_per_unit: float
    reward_per_unit: float
    risk_reward: float
    position: PositionSize
    passes_min_rr: bool
    passes_max_risk: bool
    target_basis: str = "fixed R-multiples"
    rejected_reason: str | None = None

    @property
    def is_tradeable(self) -> bool:
        return self.rejected_reason is None


def compute_stop_loss(
    entry: float, atr: float, direction: Direction, atr_multiple: float
) -> float:
    """
    ATR-based stop placement. A LONG's stop sits below entry, a SHORT's
    above — the invariant Passage 4 §4's verification worked example
    calls out by name.
    """
    if atr <= 0:
        raise ValueError("ATR must be positive to place a volatility-based stop")
    distance = atr_multiple * atr
    return entry - distance if direction == "LONG" else entry + distance


def compute_targets(
    entry: float, risk_per_unit: float, direction: Direction, r_multiples: tuple[float, ...]
) -> list[float]:
    """
    Targets as R-multiples of the actual risk distance.

    This is the fix for the constant-1.0 R:R defect: the target distance
    is now derived from the risk distance times a multiple > 1, instead
    of being an independent ATR multiple that happened to equal the stop
    distance.
    """
    if risk_per_unit <= 0:
        return []
    sign = 1.0 if direction == "LONG" else -1.0
    return [entry + sign * risk_per_unit * m for m in r_multiples]


def compute_risk_reward(
    entry: float, stop_loss: float, first_target: float
) -> float | None:
    """R:R against the first target. None when risk is zero (undefined, not infinite)."""
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None
    reward = abs(first_target - entry)
    return reward / risk


def size_position(
    entry: float,
    stop_loss: float,
    settings: RiskSettings,
    *,
    equity: float | None = None,
    max_risk_pct: float | None = None,
    lot_size: int = 1,
) -> PositionSize:
    """
    Position sizing with max-risk-per-trade enforcement.

    fixed_fractional and atr_based reach the same arithmetic here — both
    risk a fixed fraction of equity per trade; they differ in where the
    stop came from (a fixed percentage vs. an ATR multiple), which is
    decided upstream in `compute_stop_loss`. Naming both keeps the
    blueprint's own vocabulary rather than collapsing the distinction
    into one unnamed method.

    `lot_size` rounds down to a whole tradeable lot — an NSE F&O
    contract cannot be bought fractionally. Rounding down, never up, is
    what keeps the max-risk cap a genuine ceiling.
    """
    account_equity = settings.account_equity if equity is None else equity
    risk_pct = settings.max_risk_per_trade_pct if max_risk_pct is None else max_risk_pct
    method = settings.sizing_method

    if account_equity <= 0:
        return PositionSize(0, 0.0, 0.0, 0.0, method, "Account equity must be positive")
    if risk_pct <= 0:
        return PositionSize(0, 0.0, 0.0, 0.0, method, "Max risk per trade must be positive")

    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit <= 0:
        return PositionSize(0, 0.0, 0.0, 0.0, method, "Stop-loss equals entry — risk is undefined")

    risk_budget = account_equity * (risk_pct / 100.0)
    raw_quantity = risk_budget / risk_per_unit
    quantity = int(raw_quantity // lot_size) * lot_size

    if quantity <= 0:
        return PositionSize(
            0,
            0.0,
            0.0,
            0.0,
            method,
            "Risk budget is too small for one tradeable lot at this stop distance",
        )

    actual_risk = quantity * risk_per_unit
    # Enforcement, not just calculation: rounding is downward, so this
    # can only be tripped by a caller passing an inconsistent budget.
    if actual_risk > risk_budget + 1e-9:
        return PositionSize(
            0, 0.0, actual_risk, 0.0, method, "Computed size exceeds the max-risk-per-trade cap"
        )

    return PositionSize(
        quantity=quantity,
        notional=quantity * entry,
        risk_amount=actual_risk,
        risk_pct_of_equity=(actual_risk / account_equity) * 100.0,
        method=method,
    )


def select_targets(
    entry: float,
    risk_per_unit: float,
    direction: Direction,
    settings: RiskSettings,
    structural_level: float | None = None,
) -> tuple[list[float], str]:
    """
    Choose targets, preferring real market structure over an arbitrary
    multiple.

    Why structure first: price tends to stall at the nearest untested
    support/resistance, so a target placed beyond it is optimistic by
    construction. Using the structural level as target 1 is also what
    makes the comparison's "risk/reward quality" axis carry information
    — with fixed R-multiples alone, R:R is the same number on every
    setup and the axis is a constant.

    Falls back to the configured R-multiples when no usable level exists:
    no structure detected, the level sits on the wrong side of entry, or
    the level is closer than `settings.min_risk_reward` multiples of the
    risk distance. That last check matters on its own, not only because
    the downstream min-R:R gate would eventually reject the setup — a
    "target" nearer than the stop distance is not a real target, and
    reporting one would misrepresent the trade even in the
    NO_VALID_SETUP path, where callers may still inspect targets for
    diagnostic purposes. Returns the targets and a short note naming
    which basis was used, so the response can say why.
    """
    if risk_per_unit <= 0:
        return [], "no risk distance"

    sign = 1.0 if direction == "LONG" else -1.0
    multiple_targets = compute_targets(entry, risk_per_unit, direction, settings.target_r_multiples)

    if structural_level is None:
        return multiple_targets, "fixed R-multiples (no structural level detected)"

    distance = (structural_level - entry) * sign
    if distance <= 0:
        # The level is behind entry — it is not a target for this trade.
        return multiple_targets, "fixed R-multiples (structural level is behind entry)"
    if distance < risk_per_unit * settings.min_risk_reward:
        # The level exists but sits closer than the minimum acceptable
        # R:R would require — using it would produce a target that is
        # not a real candidate for this trade, not merely a low-scoring
        # one. Fall back to the fixed multiples instead.
        return (
            multiple_targets,
            "fixed R-multiples (nearest structural level is closer than the minimum R:R allows)",
        )

    # Extensions beyond the structural target keep their R-multiple
    # spacing measured from the structural first target.
    extended = [structural_level]
    for multiple in settings.target_r_multiples[1:]:
        extended.append(entry + sign * risk_per_unit * multiple)
    # Keep targets monotonically further from entry.
    ordered = [extended[0]]
    for value in extended[1:]:
        if (value - entry) * sign > (ordered[-1] - entry) * sign:
            ordered.append(value)
    return ordered, "nearest structural level"


def assess(
    *,
    direction: Direction,
    entry: float,
    atr: float,
    settings: RiskSettings,
    equity: float | None = None,
    max_risk_pct: float | None = None,
    lot_size: int = 1,
    structural_level: float | None = None,
) -> RiskAssessment:
    """
    Full assessment: stop placement, targets, R:R, size, and both gates
    (minimum R:R and max risk per trade).

    Returns an assessment with `rejected_reason` set rather than raising,
    so the caller can surface *why* a setup was declined. Declining is a
    valid outcome, not an error — Passage 1 §1's "can legitimately say
    NO VALID TRADE SETUP".
    """
    stop_loss = compute_stop_loss(entry, atr, direction, settings.atr_stop_multiple)
    risk_per_unit = abs(entry - stop_loss)
    targets, target_basis = select_targets(
        entry, risk_per_unit, direction, settings, structural_level
    )

    if not targets:
        return RiskAssessment(
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            targets=[],
            risk_per_unit=risk_per_unit,
            reward_per_unit=0.0,
            risk_reward=0.0,
            position=PositionSize(0, 0.0, 0.0, 0.0, settings.sizing_method, "No valid targets"),
            passes_min_rr=False,
            passes_max_risk=False,
            target_basis=target_basis,
            rejected_reason="Risk distance is zero — no target can be placed",
        )

    reward_per_unit = abs(targets[0] - entry)
    rr = compute_risk_reward(entry, stop_loss, targets[0])
    risk_reward = 0.0 if rr is None else rr
    passes_min_rr = risk_reward >= settings.min_risk_reward

    position = size_position(
        entry, stop_loss, settings, equity=equity, max_risk_pct=max_risk_pct, lot_size=lot_size
    )
    passes_max_risk = position.quantity > 0

    rejected_reason: str | None = None
    if not passes_min_rr:
        rejected_reason = (
            f"Risk/reward {risk_reward:.2f} is below the configured minimum "
            f"of {settings.min_risk_reward:.2f}"
        )
    elif not passes_max_risk:
        rejected_reason = position.rejected_reason or "Position sizing rejected the trade"

    return RiskAssessment(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        targets=targets,
        risk_per_unit=risk_per_unit,
        reward_per_unit=reward_per_unit,
        risk_reward=risk_reward,
        position=position,
        passes_min_rr=passes_min_rr,
        passes_max_risk=passes_max_risk,
        target_basis=target_basis,
        rejected_reason=rejected_reason,
    )