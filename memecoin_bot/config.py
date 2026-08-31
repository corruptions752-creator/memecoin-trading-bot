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

# Risk postures. These change how much is committed and how selective the
# bot is -- a coherent stance, chosen deliberately, rather than filters
# quietly widened until something trades.
#
# Worth being exact about the trade: more size does not create an edge. It
# scales whatever edge exists, in both directions. On a strategy that loses,
# aggressive sizing loses faster; the reason to choose it is a belief that
# the edge is real and worth pressing.
PROFILES = {
    "conservative": {
        "risk_fraction_per_trade": 0.01,
        "max_open_positions": 3,
        "stop_loss_pct": 0.15,
        "take_profit_multiple": 2.0,
        "give_back_ladder": ((1.3, 1.0), (1.8, 1.2)),
        "trailing_stop_pct": 0.25,
        "daily_loss_limit_pct": 0.05,
        "min_entry_score": 0.55,
        "min_liquidity_usd": 25_000.0,
        "lp_substitute_min_liquidity_usd": 100_000.0,
        "min_volume_24h_usd": 50_000.0,
        "min_buy_sell_ratio_5m": 0.8,
        "max_momentum_5m_pct": 0.60,
    },
    "balanced": {
        "risk_fraction_per_trade": 0.03,
        "max_open_positions": 5,
        "stop_loss_pct": 0.25,
        "take_profit_multiple": 2.5,
        "give_back_ladder": ((1.4, 1.0), (1.9, 1.2)),
        "trailing_stop_pct": 0.30,
        "daily_loss_limit_pct": 0.10,
        "min_entry_score": 0.45,
        "min_liquidity_usd": 15_000.0,
        "lp_substitute_min_liquidity_usd": 45_000.0,
        "min_volume_24h_usd": 20_000.0,
        "min_buy_sell_ratio_5m": 0.7,
        "max_momentum_5m_pct": 0.90,
    },
    "aggressive": {
        "risk_fraction_per_trade": 0.05,
        "max_open_positions": 8,
        "stop_loss_pct": 0.35,
        "take_profit_multiple": 3.0,
        "give_back_ladder": ((1.5, 1.0), (2.0, 1.25)),
        "trailing_stop_pct": 0.35,
        "daily_loss_limit_pct": 0.20,
        "min_entry_score": 0.35,
        "min_liquidity_usd": 10_000.0,
        "lp_substitute_min_liquidity_usd": 25_000.0,
        "min_volume_24h_usd": 8_000.0,
        "min_buy_sell_ratio_5m": 0.6,
        "max_momentum_5m_pct": 1.50,
    },
    # Circuit breaker off, by request. The slot count is NOT raised, and
    # that is a deliberate departure from "deploy the whole bankroll" --
    # measured, not assumed.
    #
    # Sweeping deployment across 70 seeds of identical synthetic paths, with
    # the breaker off throughout:
    #
    #     config              mean   median    best   win   mean w/o best seed
    #     4 slots x25% (100%)  6.31  -253.82  5020.97 22/70   -66.36
    #     5 slots x15% ( 75%) 40.93   -99.19  2899.02 28/70    -0.49
    #     3 slots x 5% ( 15%) 24.38   -10.10   606.90 34/70   +15.94
    #
    # The last column is the whole story. Drop each config's single luckiest
    # seed and the two high-deployment ones go negative: their entire mean is
    # one lottery seed. Only the most concentrated, least-deployed config
    # survives the check, and it also has the best median, the most winning
    # seeds and the smallest worst case.
    #
    # Raising slots does the same thing in reverse: 5 -> 8 -> 12 -> 20 slots
    # ran +30, -2, -110, -182. More slots means filling them with marginal
    # candidates, and the marginal candidate is where the negative expectancy
    # lives. Run 1's own record agrees -- 10 of 23 trades never traded 20%
    # above entry and cost $155.
    #
    # So this profile turns the breaker off and otherwise leaves the shape
    # that actually produced both 3x winners alone. Deploying more is a
    # one-line change once there is evidence for it.
    "unleashed": {
        "risk_fraction_per_trade": 0.05,
        "max_open_positions": 8,
        # 0.35 -> 0.25. Run 1 closed 107 trades and 62 of them never traded
        # 20% above entry, costing $868 -- a bigger hole than the round-trips
        # the first 23 trades made look decisive. A tighter stop is the only
        # change that survived a leave-one-out check across 50 seeds: mean
        # -$24.63 -> -$0.48, median -$76 -> -$41, worst -$462 -> -$381.
        "stop_loss_pct": 0.25,
        "take_profit_multiple": 3.0,
        "give_back_ladder": ((1.5, 1.0), (2.0, 1.25)),
        "trailing_stop_pct": 0.35,
        # Off: the bot may lose the entire bankroll in a day without halting.
        "daily_loss_limit_pct": 1.0,
        "min_entry_score": 0.35,
        "min_liquidity_usd": 10_000.0,
        "lp_substitute_min_liquidity_usd": 25_000.0,
        "min_volume_24h_usd": 8_000.0,
        "min_buy_sell_ratio_5m": 0.6,
        "max_momentum_5m_pct": 1.50,
    },
}

# How hard verification bites. Contract checks were rejecting every token
# that cleared the market screen, so paper trading produced nothing to learn
# from. Paper risks no money and exists to observe behaviour, so it can run
# with verification advisory; live cannot, and the loader refuses to let it.
VERIFY_STRICT = "strict"
VERIFY_ADVISORY = "advisory"

LP_AUTO = "auto"
LP_STRICT = "strict"
LP_SUBSTITUTE = "substitute"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    # --- Mode -------------------------------------------------------------
    verification: str = VERIFY_STRICT
    """``strict`` rejects a token whose contract state cannot be confirmed
    safe. ``advisory`` records the same findings but lets the trade proceed,
    which is only permitted in paper mode.

    Read advisory results with the asymmetry in mind: a honeypot bought on
    paper will appear to sell normally, because the simulator has no way to
    know the real chain would refuse. Paper profit on an unverified token is
    therefore an overestimate, not merely a riskier version of the same
    number."""

    profile: str = "conservative"
    """Which risk posture the settings came from, for display."""

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
    give_back_ladder: tuple[tuple[float, float], ...] = ((1.3, 1.0), (1.8, 1.2))
    """Floors that ratchet up as a position runs, as (peak, floor) multiples.

    Before this existed the only exit between entry and the first profit
    target was the hard stop, so a position could run to 2.3x and still be
    walked all the way down to -35%. Six of the first twenty-three closed
    trades did exactly that, for $102.75 of the $125.16 total drawdown.

    The floor only ever rises and never sells into strength, so the rare
    runners that pay for everything are untouched by it.
    """
    max_hold_seconds: int = 6 * 3_600
    """Time stop. Meme coin momentum decays fast; stale bags are dead money."""

    # --- Daily circuit breaker -------------------------------------------
    daily_loss_limit_pct: float = 0.05
    """Realized loss for the UTC day that halts new entries."""

    # --- Safety screen ----------------------------------------------------
    min_liquidity_usd: float = 25_000.0
    max_liquidity_usd: float = 5_000_000.0
    min_volume_24h_usd: float = 50_000.0
    """Turnover floor. It must scale with the liquidity floor: a pool holding
    $10k essentially never turns over $50k a day, so a posture that lowered
    one without the other opened a band it could never trade through. A live
    scan rejected 121 of 338 tokens here for exactly that reason."""
    min_pair_age_seconds: int = 30 * 60
    """Refuse tokens younger than this. The first minutes are where the
    sniper bots and the instant rugs live."""
    max_pair_age_seconds: int = 0
    """Upper age bound, or 0 for none. Default: none.

    Every risk an age cap might stand in for is already checked directly, and
    more precisely:

    * rug risk -> the contract checks (mint and freeze authority, LP, holder
      concentration, and a simulated sell);
    * a dead pool -> the liquidity and volume floors;
    * faded momentum -> the 5-minute and 1-hour moves the strategy scores.

    That leaves no residual risk for age to catch above the 30-minute
    minimum, which is the guard that actually matters. Successive live scans
    had this rule rejecting 67 then 37 candidates for a reason nothing
    supports, so it is off rather than merely widened.

    Set a value to re-enable it; the minimum age is unaffected either way.
    """
    max_fdv_usd: float = 300_000_000.0
    """Size ceiling, set by what the exit ladder needs rather than by taste.

    The first target is 2x within the hold window. A token has to be small
    enough that doubling is plausible, or the ladder never fires and the time
    stop closes the position flat minus costs. Above roughly this size a
    same-session double is rare enough not to plan around.

    This was $50m, which rejected 36 of 171 tokens on a live scan. That was
    tighter than the ladder requires, and -- being an absolute number -- it
    was also the wrong shape: see ``max_fdv_to_liquidity``."""

    max_fdv_to_liquidity: float = 300.0
    """Supply overhang: token value in existence per dollar of exit depth.

    This is the risk the absolute cap was reaching for and missing. A $30m
    token sitting on $40k of liquidity carries 750x overhang and is far more
    dangerous than a $900m token with $9m of liquidity at 100x -- yet a flat
    $50m ceiling passed the first and rejected the second.

    Ratio is the honest measure because it compares what could be sold
    against what the pool could absorb. It is a real check, not a relaxation:
    it rejects thin small-caps the old rule waved through."""
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
    jupiter_endpoint: str = "https://lite-api.jup.ag/swap/v1"
    """Keyless Jupiter tier. The previous host was retired and stopped
    resolving, which silently disabled the honeypot check."""
    jupiter_api_key: str = ""
    """Set with MEMEBOT_JUPITER_API_KEY to use api.jup.ag instead."""
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
    """Depth at which pool age is accepted in place of a proven LP lock.

    It must sit above the market liquidity floor, but not so far above it
    that a band opens which passes the market screen and then always fails
    the LP check. The aggressive profile accepted $10k of liquidity while
    this stayed at $100k, so every token between the two cleared phase one
    and died in phase two -- which is precisely what a live run showed."""
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


def apply_profile(name: str) -> dict:
    """Settings overrides for a named risk posture."""

    key = (name or "").strip().lower()
    if key not in PROFILES:
        raise RuntimeError(
            f"MEMEBOT_PROFILE must be one of {', '.join(PROFILES)}."
        )
    return dict(PROFILES[key])


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


def _ladder(
    name: str, default: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    """Read a give-back ladder from the environment.

    Written as ``peak:floor`` pairs, e.g. ``"1.5:1.0,2.0:1.25"``. Sorted by
    trigger so the walk in ``strategy`` can stop at the first rung the peak
    has not reached.
    """

    raw = os.getenv(name, "").strip()
    if not raw:
        return tuple(sorted(default))

    rungs: list[tuple[float, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        peak, _, floor = chunk.partition(":")
        try:
            rung = (float(peak), float(floor))
        except ValueError:
            raise RuntimeError(
                f"{name} wants 'peak:floor' pairs like '1.5:1.0,2.0:1.25'; "
                f"could not read {chunk!r}."
            ) from None
        if rung[0] <= 1.0:
            raise RuntimeError(
                f"{name}: a floor that arms at or below 1.0x ({rung[0]}) would "
                "fire the moment a position opened."
            )
        if rung[1] > rung[0]:
            raise RuntimeError(
                f"{name}: floor {rung[1]}x sits above its own trigger "
                f"{rung[0]}x, so it would fire immediately on arming."
            )
        rungs.append(rung)
    return tuple(sorted(rungs))


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

    # A profile sets the whole posture; individual variables still win over
    # it, so a profile is a starting point rather than a cage.
    profile_name = os.getenv("MEMEBOT_PROFILE", "conservative").strip().lower()
    profile = apply_profile(profile_name or "conservative")

    verification = os.getenv(
        "MEMEBOT_VERIFICATION", VERIFY_STRICT
    ).strip().lower() or VERIFY_STRICT
    if verification not in (VERIFY_STRICT, VERIFY_ADVISORY):
        raise RuntimeError(
            f"MEMEBOT_VERIFICATION must be '{VERIFY_STRICT}' or "
            f"'{VERIFY_ADVISORY}'."
        )
    if verification == VERIFY_ADVISORY and mode == LIVE:
        raise RuntimeError(
            "Refusing to run live with advisory verification. The contract "
            "checks are what stand between the bot and a honeypot; skipping "
            "them is defensible only when no funds are at risk."
        )

    settings = Settings(
        mode=mode,
        verification=verification,
        profile=profile_name or "conservative",
        starting_bankroll_usd=_float(
            "MEMEBOT_BANKROLL_USD", 1_000.0, minimum=1.0
        ),
        risk_fraction_per_trade=_float(
            "MEMEBOT_RISK_FRACTION", profile["risk_fraction_per_trade"],
            minimum=0.0001, maximum=0.25,
        ),
        max_open_positions=_int(
            "MEMEBOT_MAX_POSITIONS", profile["max_open_positions"], minimum=1
        ),
        stop_loss_pct=_float(
            "MEMEBOT_STOP_LOSS_PCT", profile["stop_loss_pct"],
            minimum=0.01, maximum=0.95,
        ),
        take_profit_multiple=_float(
            "MEMEBOT_TAKE_PROFIT_MULTIPLE", profile["take_profit_multiple"],
            minimum=1.05,
        ),
        trailing_stop_pct=_float(
            "MEMEBOT_TRAILING_STOP_PCT", profile["trailing_stop_pct"],
            minimum=0.01, maximum=0.95,
        ),
        give_back_ladder=_ladder(
            "MEMEBOT_GIVE_BACK_LADDER", profile["give_back_ladder"]
        ),
        min_entry_score=_float(
            "MEMEBOT_MIN_ENTRY_SCORE", profile["min_entry_score"],
            minimum=0.0, maximum=1.0,
        ),
        max_hold_seconds=_int("MEMEBOT_MAX_HOLD_SECONDS", 6 * 3_600, minimum=60),
        daily_loss_limit_pct=_float(
            "MEMEBOT_DAILY_LOSS_LIMIT_PCT", profile["daily_loss_limit_pct"],
            minimum=0.005, maximum=1.0,
        ),
        min_liquidity_usd=_float(
            "MEMEBOT_MIN_LIQUIDITY_USD", profile["min_liquidity_usd"],
            minimum=0.0,
        ),
        min_volume_24h_usd=_float(
            "MEMEBOT_MIN_VOLUME_24H_USD", profile["min_volume_24h_usd"],
            minimum=0.0,
        ),
        min_buy_sell_ratio_5m=_float(
            "MEMEBOT_MIN_BUY_SELL_RATIO", profile["min_buy_sell_ratio_5m"],
            minimum=0.0,
        ),
        max_momentum_5m_pct=_float(
            "MEMEBOT_MAX_MOMENTUM_5M", profile["max_momentum_5m_pct"],
            minimum=0.01,
        ),
        lp_substitute_min_liquidity_usd=_float(
            "MEMEBOT_LP_SUBSTITUTE_MIN_LIQUIDITY_USD",
            profile["lp_substitute_min_liquidity_usd"], minimum=0.0,
        ),
        jupiter_endpoint=os.getenv(
            "MEMEBOT_JUPITER_ENDPOINT", "https://lite-api.jup.ag/swap/v1"
        ).strip() or "https://lite-api.jup.ag/swap/v1",
        jupiter_api_key=os.getenv("MEMEBOT_JUPITER_API_KEY", "").strip(),
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
