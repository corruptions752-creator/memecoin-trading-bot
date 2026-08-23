"""Shared fixtures and fakes for the trading bot tests."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memecoin_bot.config import Settings
from memecoin_bot.models import TokenSnapshot
from memecoin_bot.safety import TokenAuthority

NOW = 1_700_000_000.0


@pytest.fixture
def settings() -> Settings:
    """Default conservative settings."""

    return Settings()


def deterministic_settings(**overrides) -> Settings:
    """Settings with every stochastic execution effect switched off.

    Engine and strategy tests are about decision logic, so they must not be
    at the mercy of a simulated sandwich attack or a dropped transaction.
    Execution realism is tested directly in test_execution.py instead.
    """

    defaults = dict(
        tx_drop_rate=0.0,
        sandwich_base_rate=0.0,
        execution_latency_seconds=0.0,
        adverse_selection_bps=0.0,
        priority_fee_volatility=0.0,
        max_slippage_pct=0.95,
        urgent_slippage_pct=0.95,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_snapshot(**overrides) -> TokenSnapshot:
    """Build a snapshot that passes the market screen unless overridden."""

    defaults = dict(
        mint="MintAddress1111111111111111111111111111111",
        symbol="TEST",
        pair_address="Pair1111",
        price_usd=0.001,
        liquidity_usd=100_000.0,
        fdv_usd=2_000_000.0,
        volume_24h_usd=500_000.0,
        volume_5m_usd=20_000.0,
        price_change_5m=0.10,
        price_change_1h=0.15,
        price_change_24h=0.40,
        buys_5m=120,
        sells_5m=80,
        pair_created_at=NOW - 6 * 3_600,
        fetched_at=NOW,
    )
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


def safe_authority(**overrides) -> TokenAuthority:
    """Contract facts that pass every check unless overridden."""

    defaults = dict(
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_locked_or_burned=True,
        top_holder_pct=0.05,
        sell_simulation_ok=True,
    )
    defaults.update(overrides)
    return TokenAuthority(**defaults)


class FakeMarket:
    """In-memory market data with scriptable prices."""

    def __init__(self, candidates=None, prices=None):
        self.candidates = list(candidates or [])
        self.prices = dict(prices or {})
        self.discover_calls = 0

    def discover(self):
        self.discover_calls += 1
        return list(self.candidates)

    def snapshot(self, mint):
        return self.prices.get(mint)

    def set_price(self, mint, price_usd, **overrides):
        """Replace the snapshot for a mint at a new price."""

        self.prices[mint] = make_snapshot(
            mint=mint, price_usd=price_usd, **overrides
        )
