"""The agents that argue from the record rather than from the tape.

These are the ones that can say a setup looks good and still refuse it,
because the last forty times it looked like this it lost. They all share
the HISTORY family: they read the same closed trades, so the lead counts
them as one line of evidence, not four.
"""

from __future__ import annotations

from ..statistics_tools import (
    MIN_MEANINGFUL_SAMPLE,
    expectancy,
    mean_without_best,
    profit_factor,
    split_train_validation,
    wilson_interval,
)
from ..trade_memory import SetupFeatures, distance
from .base import AgentReport, AnalysisContext, SignalFamily, Stance, insufficient


class HistoricalPatternAgent:
    """Finds past setups resembling this one and reports how they ended.

    The whole value is in refusing to overclaim. A neighbourhood of six
    trades is reported as six trades with an interval spanning most of the
    unit line, never as a win rate.
    """

    name = "historical"
    signal_family = SignalFamily.HISTORY
    radius = 1.5

    def analyse(self, context: AnalysisContext) -> AgentReport:
        history = context.history
        if not history:
            return insufficient(
                self.name, self.signal_family,
                "no closed trades recorded yet", evidence_n=0,
            )

        features = SetupFeatures.from_snapshot(context.snapshot)
        near = [row for row in history if distance(features, dict(row)) <= self.radius]
        n = len(near)
        if n < 5:
            return insufficient(
                self.name, self.signal_family,
                f"only {n} past setups resemble this one; too few to read",
                evidence_n=n,
            )

        wins = sum(1 for row in near if row["win"])
        rate, low, high = wilson_interval(wins, n)
        results = [row["realized_usd"] or 0.0 for row in near]
        per_trade = expectancy(results)

        notes = [
            f"resembles {n} past setups: {wins} profitable, {n - wins} losses",
            f"win rate {rate:.1%} (95% interval {low:.1%} to {high:.1%})",
            f"expectancy ${per_trade:+,.2f} per trade",
        ]
        factors_for: list[str] = []
        factors_against: list[str] = []

        if n < MIN_MEANINGFUL_SAMPLE:
            notes.append(
                f"sample under {MIN_MEANINGFUL_SAMPLE}: treated as indicative, "
                "not as a measured edge"
            )

        # The interval decides the stance, never the point estimate. A 70%
        # rate whose lower bound sits under 50% has not established anything.
        if low > 0.50 and per_trade > 0:
            factors_for.append(
                f"even the pessimistic end of the interval wins ({low:.1%})"
            )
            stance = Stance.BULLISH
        elif high < 0.40 or per_trade < 0:
            factors_against.append(
                f"these setups historically lose ${abs(per_trade):,.2f} a trade"
            )
            stance = Stance.BEARISH
        else:
            stance = Stance.NEUTRAL

        # Confidence is capped by how much evidence there actually is.
        evidence_cap = min(100.0, 40.0 + 60.0 * min(1.0, n / 100.0))
        strength = abs(rate - 0.5) * 2
        confidence = min(evidence_cap, 40.0 + 55.0 * strength)

        return AgentReport(
            agent=self.name, stance=stance, confidence=round(confidence, 1),
            signal_family=self.signal_family,
            factors_for=tuple(factors_for), factors_against=tuple(factors_against),
            notes=tuple(notes), evidence_n=n,
        )


class StrategyResearchAgent:
    """Ranks the playbooks on their own records, split by regime.

    Answers one question for the candidate: is the playbook proposing this
    trade one that has actually worked, and has it worked in conditions
    like today's?
    """

    name = "research"
    signal_family = SignalFamily.HISTORY

    def analyse(self, context: AnalysisContext) -> AgentReport:
        history = context.history
        proposed = getattr(context, "proposed_strategy", "") or ""
        if not history or not proposed:
            return insufficient(
                self.name, self.signal_family,
                "no closed trades, or no strategy proposed yet",
                evidence_n=len(history),
            )

        same = [r for r in history if r["strategy"] == proposed]
        if len(same) < 5:
            return insufficient(
                self.name, self.signal_family,
                f"{proposed} has only {len(same)} closed trades",
                evidence_n=len(same),
            )

        results = [r["realized_usd"] or 0.0 for r in same]
        wins = sum(1 for r in same if r["win"])
        rate, low, high = wilson_interval(wins, len(same))
        per_trade = expectancy(results)
        factor = profit_factor(results)
        trimmed = mean_without_best(results)

        notes = [
            f"{proposed}: {len(same)} trades, {rate:.1%} win "
            f"({low:.1%}-{high:.1%})",
            f"expectancy ${per_trade:+,.2f}, profit factor {factor:.2f}",
        ]
        factors_for: list[str] = []
        factors_against: list[str] = []

        # A record that collapses without its best trade was that trade.
        if trimmed == trimmed and per_trade > 0 and trimmed <= 0:
            factors_against.append(
                f"expectancy is carried by one trade: ${trimmed:+,.2f} without it"
            )
            notes.append("drop the single best trade and the edge disappears")

        same_regime = [r for r in same if r["regime"] == context.regime]
        if len(same_regime) >= 5:
            regime_wins = sum(1 for r in same_regime if r["win"])
            regime_rate = regime_wins / len(same_regime)
            notes.append(
                f"in {context.regime}: {regime_rate:.1%} over "
                f"{len(same_regime)} trades"
            )
            if regime_rate < 0.3:
                factors_against.append(
                    f"{proposed} does badly in {context.regime}"
                )
        else:
            notes.append(
                f"under {len(same_regime)} trades in {context.regime}: "
                "no regime-specific read"
            )

        if per_trade > 0 and factor > 1.2 and not factors_against:
            factors_for.append(f"{proposed} is profitable over {len(same)} trades")
            stance = Stance.BULLISH
        elif per_trade < 0:
            factors_against.append(
                f"{proposed} loses ${abs(per_trade):,.2f} a trade over {len(same)}"
            )
            stance = Stance.BEARISH
        else:
            stance = Stance.NEUTRAL

        cap = min(100.0, 35.0 + 65.0 * min(1.0, len(same) / 100.0))
        return AgentReport(
            agent=self.name, stance=stance,
            confidence=round(min(cap, 45.0 + 40.0 * min(1.0, abs(per_trade) / 20.0)), 1),
            signal_family=self.signal_family,
            factors_for=tuple(factors_for), factors_against=tuple(factors_against),
            notes=tuple(notes), evidence_n=len(same),
        )


class StatisticalValidationAgent:
    """Asks whether the record would survive being tested honestly.

    Splits the history chronologically and reports the held-out half
    separately. An edge that exists only in the half the rules were shaped
    on is not an edge.
    """

    name = "statistical"
    signal_family = SignalFamily.HISTORY

    def analyse(self, context: AnalysisContext) -> AgentReport:
        history = context.history
        proposed = getattr(context, "proposed_strategy", "") or ""
        same = [r for r in history if not proposed or r["strategy"] == proposed]

        if len(same) < 10:
            return insufficient(
                self.name, self.signal_family,
                f"{len(same)} trades cannot be split into train and validation",
                evidence_n=len(same),
            )

        train, validation = split_train_validation(same)
        if len(validation) < 5:
            return insufficient(
                self.name, self.signal_family,
                f"validation half holds only {len(validation)} trades",
                evidence_n=len(same),
            )

        train_results = [r["realized_usd"] or 0.0 for r in train]
        val_results = [r["realized_usd"] or 0.0 for r in validation]
        train_exp, val_exp = expectancy(train_results), expectancy(val_results)
        val_wins = sum(1 for r in validation if r["win"])
        _, val_low, val_high = wilson_interval(val_wins, len(validation))

        notes = [
            f"train {len(train)} trades: ${train_exp:+,.2f}/trade",
            f"held out {len(validation)} trades: ${val_exp:+,.2f}/trade",
            f"held-out win rate interval {val_low:.1%} to {val_high:.1%}",
        ]
        factors_for: list[str] = []
        factors_against: list[str] = []

        if train_exp > 0 and val_exp <= 0:
            factors_against.append(
                "profitable in-sample and not out-of-sample: the classic "
                "signature of a rule fitted to its own history"
            )
            stance = Stance.BEARISH
        elif val_exp > 0 and train_exp > 0:
            factors_for.append("positive in both halves")
            stance = Stance.BULLISH
        elif val_exp > 0:
            notes.append("positive out-of-sample only; too early to call")
            stance = Stance.NEUTRAL
        else:
            factors_against.append("negative in both halves")
            stance = Stance.BEARISH

        if len(same) < MIN_MEANINGFUL_SAMPLE:
            notes.append(
                f"whole sample is {len(same)} trades: below the "
                f"{MIN_MEANINGFUL_SAMPLE} needed to call anything measured"
            )

        cap = min(85.0, 30.0 + 55.0 * min(1.0, len(same) / 120.0))
        return AgentReport(
            agent=self.name, stance=stance, confidence=round(cap, 1),
            signal_family=self.signal_family,
            factors_for=tuple(factors_for), factors_against=tuple(factors_against),
            notes=tuple(notes), evidence_n=len(same),
        )


class AdversarialAgent:
    """Argues the other side. Reads the panel, not the market.

    Its job is to find the reasons this fails, and in particular to catch
    the panel agreeing with itself: several agents reading one signal is
    one opinion, however many boxes it fills.
    """

    name = "adversary"
    signal_family = SignalFamily.META

    def analyse(self, context: AnalysisContext) -> AgentReport:
        reports = context.reports
        objections: list[str] = []
        s = context.snapshot

        bulls = [r for r in reports.values() if r.stance is Stance.BULLISH]
        families = {r.signal_family for r in bulls}
        if len(bulls) >= 3 and len(families) <= 1:
            objections.append(
                f"{len(bulls)} bullish reports all read "
                f"{next(iter(families)).value}: one signal, not three"
            )

        history = reports.get("historical")
        if history is not None and history.stance is Stance.INSUFFICIENT_DATA:
            objections.append(
                "no comparable history: this setup is unproven, whatever the "
                "tape says"
            )
        elif history is not None and (history.evidence_n or 0) < MIN_MEANINGFUL_SAMPLE:
            objections.append(
                f"historical read rests on {history.evidence_n} setups"
            )

        if s.price_change_5m > 0.40:
            objections.append(
                f"up {s.price_change_5m:.0%} in five minutes: entering here "
                "buys the exit liquidity of whoever is already in"
            )
        if s.price_change_24h > 5.0:
            objections.append(f"already up {s.price_change_24h:.0%} on the day")
        if s.buys_5m + s.sells_5m < 15:
            objections.append(
                f"{s.buys_5m + s.sells_5m} trades in five minutes: every flow "
                "signal here is computed from almost nothing"
            )
        if s.age_seconds < 2 * 3_600:
            objections.append(
                f"pair is {s.age_seconds / 3_600:.1f}h old with no history to judge it on"
            )

        statistical = reports.get("statistical")
        if statistical is not None and statistical.stance is Stance.BEARISH:
            objections.append("validation agent found no out-of-sample edge")

        if not objections:
            return AgentReport(
                agent=self.name, stance=Stance.NEUTRAL, confidence=35.0,
                signal_family=self.signal_family,
                notes=("looked for the case against and did not find a strong one",),
            )

        confidence = min(90.0, 30.0 + 15.0 * len(objections))
        return AgentReport(
            agent=self.name, stance=Stance.BEARISH, confidence=confidence,
            signal_family=self.signal_family,
            factors_against=tuple(objections),
            notes=(f"{len(objections)} objections raised",),
        )
