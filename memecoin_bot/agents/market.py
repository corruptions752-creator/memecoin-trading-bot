"""The agents that read the market as it is right now.

Scanner, technical, momentum, liquidity and sentiment. They share the
present-tense data, so they declare narrow signal families and the lead
discounts them against each other rather than stacking them up.
"""

from __future__ import annotations

from .base import AgentReport, AnalysisContext, SignalFamily, Stance, insufficient


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class MarketScanner:
    """Flags what is unusual about a candidate. Never decides."""

    name = "scanner"
    signal_family = SignalFamily.PRICE_ACTION

    def analyse(self, context: AnalysisContext) -> AgentReport:
        s = context.snapshot
        found: list[str] = []
        setup = "unclassified"

        if s.price_change_5m >= 0.15:
            found.append(f"5m up {s.price_change_5m:.1%}")
            setup = "momentum breakout"
        elif s.price_change_5m <= -0.10:
            found.append(f"5m down {s.price_change_5m:.1%}")
            setup = "reversal candidate"
        if s.price_change_1h >= 0.25 and abs(s.price_change_5m) < 0.06:
            found.append(f"1h trend {s.price_change_1h:.1%} with a quiet candle")
            setup = "trend continuation"

        turnover = s.volume_to_liquidity_24h
        if turnover >= 5.0:
            found.append(f"turnover {turnover:.1f}x the pool")
            if setup == "unclassified":
                setup = "volume expansion"
        if s.age_seconds < 6 * 3_600:
            found.append(f"pair only {s.age_seconds / 3_600:.1f}h old")

        if not found:
            return AgentReport(
                agent=self.name, stance=Stance.NEUTRAL, confidence=20.0,
                signal_family=self.signal_family,
                notes=("nothing unusual about this token",), setup="none",
            )
        return AgentReport(
            agent=self.name, stance=Stance.NEUTRAL,
            confidence=_clamp(30 + 15 * len(found)),
            signal_family=self.signal_family,
            factors_for=tuple(found), setup=setup,
            notes=("scanner classifies only; it does not vote direction",),
        )


class TechnicalAnalyst:
    """Price structure and trend agreement across timeframes."""

    name = "technical"
    signal_family = SignalFamily.PRICE_ACTION

    def analyse(self, context: AnalysisContext) -> AgentReport:
        s = context.snapshot
        bull: list[str] = []
        bear: list[str] = []

        if s.price_change_24h > 0:
            bull.append(f"24h positive at {s.price_change_24h:.1%}")
        else:
            bear.append(f"24h negative at {s.price_change_24h:.1%}")

        if s.price_change_1h > 0.10:
            bull.append(f"1h trending up {s.price_change_1h:.1%}")
        elif s.price_change_1h < -0.10:
            bear.append(f"1h trending down {s.price_change_1h:.1%}")

        # Timeframe agreement is the structure signal that matters most
        # here: a 5m pop against a falling hour is a bounce far more often
        # than it is a turn.
        aligned = (s.price_change_5m > 0) == (s.price_change_1h > 0)
        if aligned and s.price_change_5m > 0:
            bull.append("5m and 1h both up: timeframes agree")
        elif not aligned and s.price_change_5m > 0:
            bear.append("5m up against a falling 1h: bounce risk")

        # Distance travelled on the day bounds how much room is left.
        if s.price_change_24h > 3.0:
            bear.append(f"already up {s.price_change_24h:.0%} on the day")

        score = 50.0 + 12.0 * len(bull) - 14.0 * len(bear)
        score = _clamp(score)
        stance = (
            Stance.BULLISH if score >= 60
            else Stance.BEARISH if score <= 40
            else Stance.NEUTRAL
        )
        return AgentReport(
            agent=self.name, stance=stance, confidence=score,
            signal_family=self.signal_family,
            factors_for=tuple(bull), factors_against=tuple(bear),
        )


class MomentumAgent:
    """Classifies where in its life a move is: early, healthy, or spent."""

    name = "momentum"
    signal_family = SignalFamily.FLOW

    def analyse(self, context: AnalysisContext) -> AgentReport:
        s = context.snapshot
        five = s.price_change_5m
        ratio = s.buy_sell_ratio_5m
        turnover = s.volume_to_liquidity_24h
        bull: list[str] = []
        bear: list[str] = []

        # Where the move sits in its own arc.
        if five <= 0:
            phase = "reversing" if s.price_change_1h > 0.15 else "absent"
        elif five > 0.60:
            phase = "overextended"
        elif five > 0.30:
            phase = "late"
        elif five > 0.08:
            phase = "healthy"
        else:
            phase = "early"

        if phase in ("early", "healthy"):
            bull.append(f"momentum {phase} at {five:.1%}")
        elif phase in ("late", "overextended"):
            bear.append(f"momentum {phase} at {five:.1%}: buying someone's exit")
        else:
            bear.append(f"momentum {phase}")

        flow = s.buys_5m + s.sells_5m
        if flow < 10:
            # A ratio computed from four trades is not a measurement.
            bear.append(f"only {flow} trades in 5m: flow is not measurable")
        elif ratio >= 1.5:
            bull.append(f"buy/sell {ratio:.2f} across {flow} trades")
        elif ratio < 0.9:
            bear.append(f"buy/sell {ratio:.2f}: sellers in control")

        if turnover >= 3.0:
            bull.append(f"turnover {turnover:.1f}x")
        elif turnover < 0.5:
            bear.append(f"turnover only {turnover:.1f}x: nobody is trading it")

        score = _clamp(50.0 + 13.0 * len(bull) - 15.0 * len(bear))
        stance = (
            Stance.BULLISH if score >= 60
            else Stance.BEARISH if score <= 40
            else Stance.NEUTRAL
        )
        return AgentReport(
            agent=self.name, stance=stance, confidence=score,
            signal_family=self.signal_family,
            factors_for=tuple(bull), factors_against=tuple(bear),
            setup=f"momentum {phase}",
            notes=(f"phase: {phase}",),
        )


class LiquidityRiskAgent:
    """Can veto. Judges what it will cost to leave, not whether to arrive."""

    name = "liquidity"
    signal_family = SignalFamily.DEPTH

    def analyse(self, context: AnalysisContext) -> AgentReport:
        s = context.snapshot
        size = context.position_size_usd or 0.0
        bull: list[str] = []
        bear: list[str] = []

        if s.liquidity_usd <= 0:
            return AgentReport(
                agent=self.name, stance=Stance.VETO, confidence=100.0,
                signal_family=self.signal_family,
                factors_against=("no pool liquidity reported",),
            )

        share = (size / s.liquidity_usd) if s.liquidity_usd else 1.0
        # Constant-product impact is roughly 2x the pool share each way, so
        # a position worth a few percent of the pool pays for itself twice.
        round_trip = 2 * share * 2
        if share > 0.05:
            return AgentReport(
                agent=self.name, stance=Stance.VETO, confidence=100.0,
                signal_family=self.signal_family,
                factors_against=(
                    f"${size:,.0f} is {share:.1%} of a ${s.liquidity_usd:,.0f} "
                    f"pool; round trip costs about {round_trip:.1%}",
                ),
            )

        if s.liquidity_usd >= 100_000:
            bull.append(f"pool ${s.liquidity_usd:,.0f}")
        elif s.liquidity_usd < 20_000:
            bear.append(f"thin pool ${s.liquidity_usd:,.0f}")

        if share < 0.005:
            bull.append(f"position is {share:.2%} of the pool")
        elif share > 0.02:
            bear.append(f"position is {share:.1%} of the pool")

        if s.volume_24h_usd < s.liquidity_usd * 0.3:
            bear.append("daily volume under a third of the pool: hard to exit")

        if s.age_seconds < 3_600:
            bear.append(f"pair {s.age_seconds / 60:.0f}m old: liquidity unproven")

        score = _clamp(55.0 + 12.0 * len(bull) - 16.0 * len(bear))
        stance = (
            Stance.BULLISH if score >= 65
            else Stance.BEARISH if score <= 40
            else Stance.NEUTRAL
        )
        return AgentReport(
            agent=self.name, stance=stance, confidence=score,
            signal_family=self.signal_family,
            factors_for=tuple(bull), factors_against=tuple(bear),
            notes=(f"estimated round-trip impact {round_trip:.2%}",),
        )


class SentimentAgent:
    """Social and catalyst read -- which this deployment cannot make.

    There is no social or news feed wired to this bot; it reads one pair
    API. The available proxies (buy/sell counts, volume) are already the
    momentum agent's evidence, so dressing them up as sentiment would
    manufacture a second vote from one signal, which is exactly the echo
    chamber the panel exists to avoid.

    So this returns INSUFFICIENT DATA every time, on purpose, until a real
    feed is attached. It is wired in so that adding one is a change to this
    class alone.
    """

    name = "sentiment"
    signal_family = SignalFamily.SOCIAL

    def analyse(self, context: AnalysisContext) -> AgentReport:
        return insufficient(
            self.name, self.signal_family,
            "no social or news feed is connected; refusing to infer sentiment "
            "from trade counts already counted by the momentum agent",
        )
