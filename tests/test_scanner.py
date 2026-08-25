"""Tests for the per-token reads the terminal displays.

The scanner's whole justification is that it invents nothing: a confidence
figure is the strategy's own score, a risk band is composed from the checks
the safety screen already runs, and anything with no source is reported as
unknown rather than filled in. These tests hold it to that.
"""

from conftest import (
    NOW, deterministic_settings, make_snapshot, safe_authority,
)

from memecoin_bot.safety import TokenAuthority, screen
from memecoin_bot.scanner import assess
from memecoin_bot.strategy import score_entry


def read(snapshot=None, authority=None, **kwargs):
    """Assess a snapshot with sane defaults."""

    settings = kwargs.pop("settings", None) or deterministic_settings()
    return assess(
        snapshot or make_snapshot(), settings, authority, **kwargs
    )


def factor(card, name):
    """The named risk factor from a card."""

    for entry in card["risk_factors"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no factor named {name}: {card['risk_factors']}")


# --- Confidence is not a second opinion ----------------------------------

def test_confidence_is_the_strategys_own_score():
    """A separate number dressed up as confidence would be a second model
    nobody trades on. It has to be the one that decides entries."""

    settings = deterministic_settings()
    snapshot = make_snapshot()
    score, notes = score_entry(snapshot, settings)

    card = read(snapshot, settings=settings)
    assert card["confidence"] == score
    assert card["why"] == list(notes)


def test_a_score_above_threshold_reads_buy():
    settings = deterministic_settings(min_entry_score=0.01)
    card = read(settings=settings)
    assert card["signal"] == "BUY"


def test_a_score_below_threshold_reads_watch():
    settings = deterministic_settings(min_entry_score=0.99)
    card = read(settings=settings)
    assert card["signal"] == "WATCH"


def test_a_failed_verdict_reads_avoid():
    snapshot = make_snapshot()
    settings = deterministic_settings(min_entry_score=0.01)
    verdict = screen(snapshot, settings, TokenAuthority())
    assert not verdict.passed

    card = read(snapshot, None, verdict=verdict, settings=settings)
    assert card["signal"] == "AVOID"
    assert card["blocked_by"] == list(verdict.failures)


def test_a_held_token_reads_held_whatever_it_scores():
    """The bot's own money outranks the model's current opinion of it."""

    settings = deterministic_settings(min_entry_score=0.99)
    card = read(settings=settings, held=True)
    assert card["signal"] == "HELD"


# --- Risk factors are measurements ---------------------------------------

def test_unchecked_contract_facts_read_unknown_not_safe():
    """No authority means nothing was read from the chain. Treating that as
    a pass is how an unverified token gets a clean badge."""

    card = read(authority=None)
    for name in (
        "Mint authority", "Freeze authority", "Holder concentration",
        "Sell simulation",
    ):
        assert factor(card, name)["level"] == "unknown"
        assert factor(card, name)["detail"] == "not checked"


def test_a_live_mint_authority_is_a_danger():
    card = read(authority=safe_authority(mint_authority_revoked=False))
    assert factor(card, "Mint authority")["level"] == "danger"


def test_slippage_is_measured_on_the_size_that_would_actually_trade():
    """Price impact is a function of order size, so a figure computed at
    some nominal size would understate what the bot itself pays."""

    snapshot = make_snapshot(liquidity_usd=20_000.0)
    small = read(snapshot, safe_authority(), position_size_usd=50.0)
    large = read(snapshot, safe_authority(), position_size_usd=5_000.0)

    small_impact = factor(small, "Slippage")["detail"]
    large_impact = factor(large, "Slippage")["detail"]
    assert "$50 order" in small_impact
    assert "$5,000 order" in large_impact

    to_pct = lambda text: float(text.split("%")[0])
    assert to_pct(large_impact) > to_pct(small_impact)


def test_no_position_size_means_no_slippage_factor():
    """Impact on an order that was never sized is not a measurement."""

    card = read(authority=safe_authority(), position_size_usd=0.0)
    names = [f["name"] for f in card["risk_factors"]]
    assert "Slippage" not in names


# --- The band is not an average ------------------------------------------

def test_one_danger_sinks_the_band():
    """It takes a single unrevoked mint authority to lose everything, so a
    row of green cannot average a rug back to acceptable."""

    clean = read(authority=safe_authority())
    assert clean["risk_band"] == "LOW"

    rugged = read(authority=safe_authority(sell_simulation_ok=False))
    assert rugged["risk_band"] == "DANGER"
    assert rugged["risk_score"] <= 35


def test_the_band_orders_from_low_to_danger():
    bands = {}
    for name, authority in (
        ("clean", safe_authority()),
        ("unknown", None),
        ("rugged", safe_authority(freeze_authority_revoked=False)),
    ):
        card = read(authority=authority)
        bands[name] = card["risk_score"]

    assert bands["clean"] > bands["unknown"] > bands["rugged"]


def test_a_thin_pool_is_flagged_without_any_chain_data():
    settings = deterministic_settings(min_liquidity_usd=50_000.0)
    card = read(make_snapshot(liquidity_usd=1_000.0), settings=settings)
    assert factor(card, "Liquidity depth")["level"] == "danger"


# --- Badges are earned ----------------------------------------------------

def test_every_badge_traces_to_a_measurement():
    card = read(
        make_snapshot(
            price_change_5m=0.40, buys_5m=300, sells_5m=50,
            volume_24h_usd=5_000_000.0, pair_created_at=NOW - 600,
        ),
        safe_authority(),
        settings=deterministic_settings(min_entry_score=0.01),
    )
    badges = set(card["badges"])
    assert "HIGH MOMENTUM" in badges     # 40% over five minutes
    assert "BUY PRESSURE" in badges      # six buys per sell
    assert "HIGH VOLUME" in badges       # 50x turnover
    assert "NEW PAIR" in badges          # ten minutes old
    assert "AI SIGNAL" in badges         # scored above threshold


def test_low_risk_is_only_awarded_when_nothing_is_flagged():
    """Fifty times turnover is a warning even on a clean contract, so a
    momentum token does not get to wear the same badge as a calm one."""

    hot = read(
        make_snapshot(volume_24h_usd=5_000_000.0), safe_authority(),
        position_size_usd=50.0,
    )
    assert "LOW RISK" not in hot["badges"]

    calm = read(
        make_snapshot(volume_24h_usd=300_000.0), safe_authority(),
        position_size_usd=50.0,
    )
    assert "LOW RISK" in calm["badges"]
    assert calm["risk_band"] == "LOW"


def test_a_quiet_token_earns_no_momentum_badges():
    card = read(make_snapshot(
        price_change_5m=0.0, buys_5m=10, sells_5m=10,
        volume_24h_usd=10_000.0, pair_created_at=NOW - 90 * 86_400,
    ))
    badges = set(card["badges"])
    assert not badges & {"HIGH MOMENTUM", "BUY PRESSURE", "HIGH VOLUME", "NEW PAIR"}


def test_a_dangerous_token_is_labelled_as_one():
    card = read(authority=safe_authority(sell_simulation_ok=False))
    assert "DANGER" in card["badges"]
    assert "LOW RISK" not in card["badges"]


# --- Shape ----------------------------------------------------------------

def test_a_card_carries_everything_the_terminal_renders():
    card = read(authority=safe_authority(), position_size_usd=50.0)
    for key in (
        "mint", "symbol", "price", "change_5m", "change_1h", "change_24h",
        "liquidity", "fdv", "volume_24h", "volume_5m", "buys_5m", "sells_5m",
        "buy_ratio", "turnover", "age_hours", "confidence", "why", "signal",
        "risk_band", "risk_score", "risk_factors", "badges", "blocked_by",
    ):
        assert key in card, f"card is missing {key}"


def test_a_card_is_json_serialisable():
    """It is written to SQLite and served over HTTP as JSON."""

    import json

    card = read(authority=safe_authority(), position_size_usd=50.0)
    assert json.loads(json.dumps(card))["symbol"] == card["symbol"]
