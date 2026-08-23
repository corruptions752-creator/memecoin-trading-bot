"""Tests for the offline simulator.

The simulator's job is to exercise the risk rules, so these tests check that
it stays honest -- in particular that its price generator does not quietly
manufacture an upward drift.
"""

import math
import random
import statistics

from memecoin_bot.config import Settings
from memecoin_bot.simulate import _FATES, _make_path, run_simulation, run_sweep


def test_a_simulation_produces_a_report():
    report = run_simulation(tokens=20, steps=60, seed=1)
    assert "SIMULATION" in report
    assert "Starting capital" in report


def test_a_simulation_is_deterministic_for_a_seed():
    assert run_simulation(tokens=20, steps=60, seed=3) == run_simulation(
        tokens=20, steps=60, seed=3
    )


def test_different_seeds_give_different_results():
    assert run_simulation(tokens=20, steps=60, seed=1) != run_simulation(
        tokens=20, steps=60, seed=2
    )


def test_the_sweep_reports_a_spread():
    report = run_sweep(seeds=3, tokens=15, steps=50)
    assert "Losing runs" in report
    assert "Worst drawdown" in report


def test_price_steps_are_symmetric_in_log_space():
    """Regression: a floor on losing steps invents free money.

    An earlier generator used ``max(0.05, 1 + gauss(mu, sigma))``, which
    truncated the downside while leaving the upside unbounded. That made every
    seed profitable. The realized drift must track the configured drift.
    """

    rng = random.Random(11)
    for kind, (_, drift, _) in _FATES.items():
        if kind == "rug":
            continue  # rugs are a scripted event, not a drift
        samples = []
        for index in range(60):
            path = _make_path(rng, index, 400)
            if path.kind != kind:
                continue
            steps = [
                math.log(b / a)
                for a, b in zip(path.prices, path.prices[1:])
                if a > 0 and b > 0
            ]
            samples.extend(steps)
        if len(samples) < 200:
            continue
        realized = statistics.mean(samples)
        assert abs(realized - drift) < 0.01, (
            f"{kind}: realized drift {realized:.4f} != configured {drift:.4f}"
        )


def test_the_universe_has_negative_expected_drift():
    """The token mix must be unkind, or the simulator proves nothing."""

    expected = sum(
        probability * drift
        for kind, (probability, drift, _) in _FATES.items()
        if kind != "rug"
    )
    assert expected < 0


def test_rugs_destroy_both_price_and_liquidity():
    rng = random.Random(5)
    rugs = [
        path for path in (_make_path(rng, i, 120) for i in range(80))
        if path.kind == "rug"
    ]
    assert rugs, "expected the mix to contain rugs"
    for path in rugs:
        assert path.prices[-1] < path.prices[0] * 0.01
        assert path.liquidity[-1] < path.liquidity[0] * 0.5


def test_risk_limits_bound_the_worst_run():
    """Conservative sizing must keep any single sweep out of a deep hole."""

    report = run_sweep(Settings(), seeds=8, tokens=40, steps=120)
    worst_line = [l for l in report.splitlines() if "Worst drawdown" in l][0]
    worst = float(worst_line.split(":")[1].strip().rstrip("%"))
    assert worst > -50.0, report
