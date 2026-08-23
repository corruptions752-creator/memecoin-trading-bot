"""Offline simulation against synthetic price paths.

Paper mode still depends on a live feed and real time. This module compresses
the same engine, safety screen, and exit ladder into a few seconds so the risk
rules can be inspected before anything is connected to the market.

The generated outcome distribution is deliberately unkind, and roughly matches
what the category actually does: most tokens fade or die, a minority trend,
and a few rug outright. A strategy that only looks good on generous synthetic
data has told you nothing.
"""

from dataclasses import dataclass
import math
import random

from .broker import PaperBroker
from .config import Settings
from .engine import TradingEngine
from .models import TokenSnapshot
from .reporting import summarize
from .risk import RiskManager
from .safety import TokenAuthority
from .store import Store

START = 1_700_000_000.0
STEP_SECONDS = 300


@dataclass
class TokenPath:
    """A synthetic token and the price path it will follow."""

    mint: str
    symbol: str
    prices: list[float]
    liquidity: list[float]
    kind: str


# Per-5-minute-step log drift and volatility for each fate.
#
# These are log-space so that up and down moves are symmetric. An earlier
# version multiplied by ``max(0.05, 1 + gauss(...))``, which floored losing
# steps at -95% while leaving winning steps unbounded. That asymmetry alone
# manufactured a positive drift and made every seed profitable, which is not
# something any meme coin strategy does. Expected drift is negative almost
# everywhere here on purpose: the category's base rate is loss.
_FATES = {
    #          probability, log drift, log volatility
    "rug":    (0.20, +0.004, 0.07),
    "fade":   (0.40, -0.010, 0.06),
    "chop":   (0.28, -0.001, 0.07),
    "trend":  (0.12, +0.006, 0.09),
}


def _draw_fate(rng: random.Random) -> str:
    """Pick a fate according to the configured probabilities."""

    roll = rng.random()
    cumulative = 0.0
    for kind, (probability, _, _) in _FATES.items():
        cumulative += probability
        if roll < cumulative:
            return kind
    return "chop"


def _make_path(rng: random.Random, index: int, steps: int) -> TokenPath:
    """Generate one token whose fate is drawn from a realistic mix."""

    kind = _draw_fate(rng)
    _, drift, volatility = _FATES[kind]

    price = rng.uniform(0.0001, 0.01)
    liquidity = rng.uniform(40_000, 400_000)
    prices, liquidities = [price], [liquidity]

    rug_at = rng.randint(3, max(4, steps // 2)) if kind == "rug" else None

    for step in range(1, steps):
        if rug_at is not None and step >= rug_at:
            # The pool is pulled: price and depth both go to effectively zero.
            price *= 0.02
            liquidity *= 0.01
            prices.append(max(price, 1e-12))
            liquidities.append(max(liquidity, 1.0))
            continue

        price *= math.exp(rng.gauss(drift, volatility))
        liquidity *= math.exp(rng.gauss(-0.001, 0.03))
        prices.append(max(price, 1e-12))
        liquidities.append(max(liquidity, 1.0))

    return TokenPath(
        mint=f"SimMint{index:04d}",
        symbol=f"SIM{index:03d}",
        prices=prices,
        liquidity=liquidities,
        kind=kind,
    )


class SimulatedMarket:
    """Replays generated paths as a :class:`~memecoin_bot.market.MarketData`."""

    def __init__(self, paths: list[TokenPath]) -> None:
        self.paths = {path.mint: path for path in paths}
        self.step = 0

    def _snapshot(self, path: TokenPath) -> TokenSnapshot:
        """Build the snapshot for a path at the current step."""

        index = min(self.step, len(path.prices) - 1)
        price = path.prices[index]
        previous = path.prices[max(0, index - 1)]
        hour_ago = path.prices[max(0, index - 12)]
        change_5m = (price / previous - 1.0) if previous > 0 else 0.0
        change_1h = (price / hour_ago - 1.0) if hour_ago > 0 else 0.0
        liquidity = path.liquidity[index]

        # Buy/sell flow correlates with direction, as it does in reality.
        buys = 100 + int(max(-90, min(400, change_5m * 900)))
        return TokenSnapshot(
            mint=path.mint,
            symbol=path.symbol,
            pair_address=f"Pair{path.mint}",
            price_usd=price,
            liquidity_usd=liquidity,
            fdv_usd=liquidity * 20,
            volume_24h_usd=liquidity * 4,
            volume_5m_usd=liquidity * 0.05,
            price_change_5m=change_5m,
            price_change_1h=change_1h,
            price_change_24h=change_1h,
            buys_5m=max(1, buys),
            sells_5m=100,
            pair_created_at=START - 3 * 3_600,
            fetched_at=START + self.step * STEP_SECONDS,
        )

    def discover(self) -> list[TokenSnapshot]:
        return [self._snapshot(path) for path in self.paths.values()]

    def snapshot(self, mint: str) -> TokenSnapshot | None:
        path = self.paths.get(mint)
        return self._snapshot(path) if path else None


class _CleanAuthority:
    """Treats every simulated mint as contract-clean.

    Rug risk is modelled in the price paths instead, which is where the exit
    rules have to catch it.
    """

    def fetch(self, mint: str) -> TokenAuthority:
        return TokenAuthority(
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
            lp_locked_or_burned=True,
            top_holder_pct=0.05,
            sell_simulation_ok=True,
        )


def run_simulation(
    settings: Settings | None = None,
    *,
    tokens: int = 60,
    steps: int = 200,
    seed: int = 7,
) -> str:
    """Run the engine over synthetic paths and return a report."""

    settings = settings or Settings()
    rng = random.Random(seed)
    paths = [_make_path(rng, index, steps) for index in range(tokens)]
    market = SimulatedMarket(paths)
    store = Store(":memory:")

    risk = RiskManager.start(settings, START)
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=seed), risk, store,
        _CleanAuthority(),
    )

    for step in range(steps):
        market.step = step
        engine.run_cycle(START + step * STEP_SECONDS)

    market.step = steps - 1
    engine.close_all()

    performance = summarize(store, settings.starting_bankroll_usd)
    kinds: dict[str, int] = {}
    for path in paths:
        kinds[path.kind] = kinds.get(path.kind, 0) + 1

    ending = risk.bankroll_usd
    start_bankroll = settings.starting_bankroll_usd
    lines = [
        "=" * 58,
        f"SIMULATION — {tokens} tokens over "
        f"{steps * STEP_SECONDS / 3_600:.0f} simulated hours",
        "=" * 58,
        "Universe        : "
        + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())),
        f"Starting capital: ${start_bankroll:,.2f}",
        f"Ending capital  : ${ending:,.2f}",
        f"Return          : {(ending / start_bankroll - 1):+.2%}",
        "",
        performance.render(),
        "",
        "Fills carry real constant-product impact, latency, adverse",
        "selection, sandwich risk, failed transactions and Solana fees.",
        "What stays synthetic is the price paths themselves, so read this",
        "as evidence the risk rules fire — not as a forecast.",
    ]
    return "\n".join(lines)


def run_sweep(
    settings: Settings | None = None,
    *,
    seeds: int = 20,
    tokens: int = 60,
    steps: int = 200,
) -> str:
    """Run many simulations and report the spread.

    A single run says nothing. One lucky seed can show a large gain from a
    strategy that loses on most others, which is exactly how backtests get
    used to sell bots. The spread and the losing-run count are the numbers
    that matter.
    """

    settings = settings or Settings()
    returns: list[float] = []
    worst_drawdown = 0.0
    fills = failures = sandwiches = 0
    fees = impact = 0.0

    for seed in range(1, seeds + 1):
        rng = random.Random(seed)
        paths = [_make_path(rng, index, steps) for index in range(tokens)]
        market = SimulatedMarket(paths)
        store = Store(":memory:")
        risk = RiskManager.start(settings, START)
        engine = TradingEngine(
            settings, market, PaperBroker(settings, seed=seed), risk, store,
            _CleanAuthority(),
        )

        peak = settings.starting_bankroll_usd
        for step in range(steps):
            market.step = step
            engine.run_cycle(START + step * STEP_SECONDS)
            equity = risk.bankroll_usd
            peak = max(peak, equity)
            worst_drawdown = min(worst_drawdown, equity / peak - 1.0)

        market.step = steps - 1
        engine.close_all()
        returns.append(risk.bankroll_usd / settings.starting_bankroll_usd - 1.0)

        broker = engine.broker
        fills += len(broker.fills)
        failures += len(broker.failures)
        sandwiches += sum(
            1 for r in getattr(broker, "sandwich_log", []) if r
        )
        fees += sum(f.fee_usd for f in broker.fills)
        fees += sum(r.network_fee_usd for r in broker.failures)
        impact += sum(f.slippage_usd for f in broker.fills)
        store.close()

    returns.sort()
    losing = sum(1 for value in returns if value < 0)
    mean = sum(returns) / len(returns)
    median = returns[len(returns) // 2]

    attempted = fills + failures
    failure_rate = failures / attempted if attempted else 0.0
    sandwich_rate = sandwiches / fills if fills else 0.0

    return "\n".join([
        "=" * 58,
        f"SWEEP — {seeds} independent runs",
        "=" * 58,
        f"Mean return     : {mean:+.2%}",
        f"Median return   : {median:+.2%}",
        f"Best / worst    : {returns[-1]:+.2%} / {returns[0]:+.2%}",
        f"Losing runs     : {losing}/{seeds}",
        f"Worst drawdown  : {worst_drawdown:.2%}",
        "",
        "Execution reality:",
        f"  transactions attempted : {attempted:,}",
        f"  failed                 : {failures:,} ({failure_rate:.1%})",
        f"  sandwiched buys        : {sandwiches:,} ({sandwich_rate:.1%} of fills)",
        f"  paid in fees           : ${fees:,.2f}",
        f"  lost to price impact   : ${impact:,.2f}",
        "",
        "If the mean is not clearly positive, the strategy has no edge and",
        "no amount of position sizing will create one. Sizing controls how",
        "fast you lose, never whether you win.",
    ])
