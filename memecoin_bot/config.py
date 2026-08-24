"""Environment-backed configuration for the trading bot.

Every risk number is a setting rather than a literal buried in the strategy,
so limits can be tuned without editing logic. Defaults are the conservative
profile: 1% of bankroll per trade, three open positions, a 15% stop, half the
position off at 2x, and a 5% daily loss limit that halts trading.
"""

from dataclasses import dataclass
import os

PAPER = "paper"
LIVE = "live"

LP_AUTO = "auto"
LP_STRICT = "strict"
LP_SUBSTITUTE = "substitute"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    # --- Mode -------------------------------------------------------------
    mode: str = PAPER
    """Either ``paper`` or ``live``. Live additionally requires
    ``MEMEBOT_I_UNDERSTAND_THE_RISK=yes`` and a funded wallet key."""

    # --- Bankroll ---------------------------------------------------------
    starting_bankroll_usd: float = 1_000.0
    """Simulated starting capital in paper mode."""

    # --- Position sizing --------------------------------------------------
    risk_fraction_per_trade: float = 0.01
    max_open_positions: int = 3
    min_position_usd: float = 5.0

    # --- Exits ------------------------------------------------------------
    stop_loss_pct: float = 0.15
    """Hard stop, as a fraction below the entry price."""
    take_profit_multiple: float = 2.0
    """First profit target, as a multiple of the entry price."""
    take_profit_fraction: float = 0.50
    """Fraction of the position sold at the first target."""
    trailing_stop_pct: float = 0.25
    """After the first target, trail the remainder this far below its peak."""
    max_hold_seconds: int = 6 * 3_600
    """Time stop. Meme coin momentum decays fast; stale bags are dead money."""

    # --- Daily circuit breaker -------------------------------------------
    daily_loss_limit_pct: float = 0.05
    """Realized loss for the UTC day that halts new entries."""

    # --- Safety screen ----------------------------------------------------
    min_liquidity_usd: float = 25_000.0
    max_liquidity_usd: float = 5_000_000.0
    min_volume_24h_usd: float = 50_000.0
    min_pair_age_seconds: int = 30 * 60
    """Refuse tokens younger than this. The first minutes are where the
    sniper bots and the instant rugs live."""
    max_pair_age_seconds: int = 7 * 24 * 3_600
    max_fdv_usd: float = 50_000_000.0
    min_buy_sell_ratio_5m: float = 0.8
    liquidity_exit_floor_pct: float = 0.5
    """Exit if pool liquidity falls this far below its level at entry."""

    # --- Entry scoring ----------------------------------------------------
    min_entry_score: float = 0.55
    min_momentum_5m_pct: float = 0.02
    max_momentum_5m_pct: float = 0.60
    """Refuse to buy a candle that has already gone vertical. Chasing a
    spike is the single most reliable way to become someone's exit."""

    # --- Execution realism -------------------------------------------------
    # These model the gap between a decision and a confirmed fill. Every one
    # of them makes results worse; that is deliberate. A strategy that only
    # survives without them was never viable.
    sol_price_usd: float = 150.0
    """Used to price network fees and rent. Set it near the real SOL price;
    fees are denominated in SOL, not dollars."""
    pool_fee_bps: float = 25.0
    """AMM swap fee. Raydium and Orca standard pools charge 0.25%."""
    priority_fee_lamports: float = 200_000.0
    """Priority fee per transaction. Meme coin trading is uncompetitive
    below roughly this level."""
    priority_fee_volatility: float = 0.8
    """Lognormal spread on the priority fee. Fees spike under congestion,
    which is exactly when the bot most wants to transact."""
    execution_latency_seconds: float = 1.2
    """Median decision-to-confirmation time."""
    adverse_selection_bps: float = 40.0
    """Expected adverse price move while a transaction is in flight. The bot
    buys strength, and by the time a signal is visible to us it is visible to
    everyone else too."""
    max_slippage_pct: float = 0.05
    """Ordinary slippage tolerance. Exceeding it fails the transaction."""
    urgent_slippage_pct: float = 0.25
    """Tolerance for stop-loss and rug exits, where not getting out is worse
    than getting out badly."""
    tx_drop_rate: float = 0.04
    """Share of transactions that never land at all."""
    sandwich_base_rate: float = 0.25
    """Chance of being sandwiched on a buy worth 1% of the pool, scaled by
    actual size. Sells are not sandwiched; attackers front-run buys."""
    sandwich_size_multiple: float = 2.0
    """Attacker order size as a multiple of ours."""

    # --- On-chain verification --------------------------------------------
    rpc_endpoint: str = "https://api.mainnet-beta.solana.com"
    """Solana JSON-RPC endpoint. The public one rate limits hard; a paid
    endpoint is required before live trading, because a throttled safety
    check fails closed and the bot simply stops finding candidates."""
    jupiter_endpoint: str = "https://quote-api.jup.ag/v6"
    max_sell_price_impact_pct: float = 0.15
    """A sell quote worse than this counts as unsellable."""
    sell_probe_tokens: float = 1_000.0
    """Whole tokens to quote when testing whether a position can be sold."""
    authority_cache_seconds: int = 900
    """A revoked authority cannot be un-revoked, so caching is safe."""

    lp_lock_policy: str = "auto"
    """``auto`` (default) uses ``substitute`` in paper mode and ``strict`` in
    live mode. ``strict`` rejects any token whose LP lock cannot be proven --
    correct, but since LP lock is not provable from pair data it rejects
    *everything*, so a strict paper run makes no trades at all and teaches
    nothing. ``substitute`` accepts pool depth and age as a weaker proxy.
    Paper risks no money, so observing behaviour beats refusing to act;
    live keeps the strict rule."""
    lp_substitute_min_liquidity_usd: float = 100_000.0
    lp_substitute_min_age_seconds: int = 6 * 3_600

    # --- Re-entry control -------------------------------------------------
    reentry_cooldown_seconds: int = 6 * 3_600
    """After exiting a mint, refuse to buy it again for this long. Without
    this the bot stops out at -15% and immediately re-buys the same falling
    token, converting one bad trade into a grinding loop."""
    ban_after_liquidity_collapse: bool = True
    """A pool that drained under a position is a rug. Never touch it again."""

    # --- Loop -------------------------------------------------------------
    poll_seconds: int = 30
    request_timeout_seconds: float = 10.0
    database_path: str = "memecoin_bot/data/trading.sqlite3"
    quote_mint: str = "So11111111111111111111111111111111111111112"
    """Wrapped SOL, the quote asset for Solana meme coin pairs."""


def resolve_lp_policy(settings: "Settings") -> str:
    """The LP policy actually in force, resolving ``auto`` by mode.

    Strict is the correct rule and the live default. It is not the paper
    default because LP lock cannot be proven from pair data, so strict paper
    trading makes zero trades -- which looks identical to a broken bot and
    teaches nothing about the strategy.
    """

    if settings.lp_lock_policy != LP_AUTO:
        return settings.lp_lock_policy
    return LP_STRICT if settings.mode == LIVE else LP_SUBSTITUTE


def _float(name: str, default: float, *, minimum: float = 0.0,
           maximum: float | None = None) -> float:
    """Read a bounded float from the environment."""

    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number.") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}.")
    return value


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a bounded integer from the environment."""

    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a whole number.") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}.")
    return value


def load_settings() -> Settings:
    """Load settings, failing clearly when configuration is contradictory."""

    mode = os.getenv("MEMEBOT_MODE", PAPER).strip().lower() or PAPER
    if mode not in (PAPER, LIVE):
        raise RuntimeError(
            f"MEMEBOT_MODE must be '{PAPER}' or '{LIVE}', not {mode!r}."
        )

    if mode == LIVE:
        acknowledged = os.getenv(
            "MEMEBOT_I_UNDERSTAND_THE_RISK", ""
        ).strip().lower() in _TRUTHY
        if not acknowledged:
            raise RuntimeError(
                "Refusing to start in live mode. Live trading spends real "
                "funds on assets that frequently go to zero. Set "
                "MEMEBOT_I_UNDERSTAND_THE_RISK=yes to confirm you accept "
                "that, and only after reviewing paper results."
            )

    settings = Settings(
        mode=mode,
        starting_bankroll_usd=_float(
            "MEMEBOT_BANKROLL_USD", 1_000.0, minimum=1.0
        ),
        risk_fraction_per_trade=_float(
            "MEMEBOT_RISK_FRACTION", 0.01, minimum=0.0001, maximum=0.25
        ),
        max_open_positions=_int("MEMEBOT_MAX_POSITIONS", 3, minimum=1),
        stop_loss_pct=_float(
            "MEMEBOT_STOP_LOSS_PCT", 0.15, minimum=0.01, maximum=0.95
        ),
        take_profit_multiple=_float(
            "MEMEBOT_TAKE_PROFIT_MULTIPLE", 2.0, minimum=1.05
        ),
        trailing_stop_pct=_float(
            "MEMEBOT_TRAILING_STOP_PCT", 0.25, minimum=0.01, maximum=0.95
        ),
        max_hold_seconds=_int("MEMEBOT_MAX_HOLD_SECONDS", 6 * 3_600, minimum=60),
        daily_loss_limit_pct=_float(
            "MEMEBOT_DAILY_LOSS_LIMIT_PCT", 0.05, minimum=0.005, maximum=1.0
        ),
        min_liquidity_usd=_float(
            "MEMEBOT_MIN_LIQUIDITY_USD", 25_000.0, minimum=0.0
        ),
        rpc_endpoint=os.getenv(
            "MEMEBOT_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
        ).strip() or "https://api.mainnet-beta.solana.com",
        lp_lock_policy=os.getenv("MEMEBOT_LP_POLICY", "auto").strip().lower()
        or "auto",
        reentry_cooldown_seconds=_int(
            "MEMEBOT_REENTRY_COOLDOWN_SECONDS", 6 * 3_600, minimum=0
        ),
        poll_seconds=_int("MEMEBOT_POLL_SECONDS", 30, minimum=5),
        database_path=os.getenv(
            "MEMEBOT_DB_PATH", "memecoin_bot/data/trading.sqlite3"
        ).strip() or "memecoin_bot/data/trading.sqlite3",
    )

    if settings.take_profit_fraction >= 1.0:
        raise RuntimeError("take_profit_fraction must leave a runner behind.")
    if settings.lp_lock_policy not in ("auto", "strict", "substitute"):
        raise RuntimeError(
            "MEMEBOT_LP_POLICY must be 'auto', 'strict' or 'substitute'."
        )
    return settings
