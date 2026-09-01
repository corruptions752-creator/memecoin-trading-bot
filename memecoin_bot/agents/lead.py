"""Position sizing and the final decision.

The lead does not average the panel. Averaging rewards filling boxes: five
agents reading the same candle would outvote one agent reading a hundred
closed trades. Instead the reports are collapsed by signal family -- one
voice per independent line of evidence -- and the history family, being
the only one that knows how setups like this actually ended, carries the
most weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..statistics_tools import MIN_MEANINGFUL_SAMPLE
from .base import AgentReport, AnalysisContext, SignalFamily, Stance

# What one independent line of evidence is worth. History outweighs the
# tape because it is the only family that has been marked against reality.
FAMILY_WEIGHT = {
    SignalFamily.HISTORY: 1.0,
    SignalFamily.PRICE_ACTION: 0.55,
    SignalFamily.FLOW: 0.55,
    SignalFamily.DEPTH: 0.7,
    SignalFamily.SOCIAL: 0.4,
    SignalFamily.META: 0.0,   # the adversary is applied as a penalty, not a vote
}


class Decision(str, Enum):
    PAPER_TRADE = "PAPER TRADE"
    WATCH = "WATCH"
    REJECT = "REJECT"


class Conviction(str, Enum):
    NONE = "no position"
    SMALL = "small"
    NORMAL = "normal"
    HIGH = "high conviction"


@dataclass
class PanelVerdict:
    """The lead's output, and the reasoning that produced it."""

    symbol: str
    decision: Decision
    confidence: float
    strategy: str
    regime: str
    conviction: Conviction
    size_multiplier: float
    risk: str
    historical_win_rate: float | None
    sample_size: int
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    invalidation: tuple[str, ...] = ()
    reports: dict = field(default_factory=dict)
    vetoed_by: str = ""

    def render(self) -> str:
        bar = "━" * 40
        wr = (
            f"{self.historical_win_rate:.1%}" if self.historical_win_rate is not None
            else "INSUFFICIENT DATA"
        )
        lines = [
            bar, "MULTI-AGENT ANALYSIS", bar,
            f"Asset            : {self.symbol}",
            f"Market regime    : {self.regime}",
            f"Strategy         : {self.strategy or 'none'}",
            f"Historical matches: {self.sample_size}",
            f"Historical win   : {wr}",
            f"Risk             : {self.risk}",
            "",
        ]
        for name, report in sorted(self.reports.items()):
            if report.stance is Stance.INSUFFICIENT_DATA:
                lines.append(f"  {name:<12} INSUFFICIENT DATA")
            else:
                lines.append(
                    f"  {name:<12} {report.stance.value:<10} {report.confidence:>5.0f}/100"
                )
        lines += [
            "",
            f"FINAL CONFIDENCE : {self.confidence:.0f}/100",
            f"DECISION         : {self.decision.value}",
            f"POSITION         : {self.conviction.value} "
            f"({self.size_multiplier:.2f}x normal)",
        ]
        if self.vetoed_by:
            lines.append(f"VETOED BY        : {self.vetoed_by}")
        if self.reasons:
            lines.append("WHY:")
            lines += [f"  - {r}" for r in self.reasons]
        if self.risks:
            lines.append("MAIN RISKS:")
            lines += [f"  - {r}" for r in self.risks]
        if self.invalidation:
            lines.append("INVALIDATION:")
            lines += [f"  - {r}" for r in self.invalidation]
        lines.append(bar)
        return "\n".join(lines)


class LeadTrader:
    """Weighs the panel and decides."""

    name = "lead"

    def decide(self, context: AnalysisContext) -> PanelVerdict:
        reports: dict[str, AgentReport] = dict(context.reports)
        s = context.snapshot

        veto = next(
            (r for r in reports.values() if r.stance is Stance.VETO), None
        )

        history = reports.get("historical")
        sample = (history.evidence_n or 0) if history else 0
        win_rate = None
        if history is not None and history.stance is not Stance.INSUFFICIENT_DATA:
            for note in history.notes:
                if note.startswith("win rate "):
                    win_rate = float(note.split()[2].rstrip("%")) / 100.0
                    break

        # --- Collapse to one voice per independent family -----------------
        # Within a family the most confident report speaks; families are
        # then combined by weight. Two price-action agents agreeing add
        # nothing, which is the point.
        by_family: dict[SignalFamily, AgentReport] = {}
        for report in reports.values():
            if report.stance in (Stance.INSUFFICIENT_DATA, Stance.VETO):
                continue
            if report.signal_family is SignalFamily.META:
                continue
            best = by_family.get(report.signal_family)
            if best is None or report.confidence > best.confidence:
                by_family[report.signal_family] = report

        weighted = 0.0
        total_weight = 0.0
        for family, report in by_family.items():
            weight = FAMILY_WEIGHT.get(family, 0.5)
            # A report at 50 is neutral; map to -1..+1 around that.
            lean = report.directional * (report.confidence - 50.0) / 50.0
            weighted += weight * lean
            total_weight += weight

        base = 50.0 + 45.0 * (weighted / total_weight if total_weight else 0.0)

        # --- The adversary is a penalty, never a vote ---------------------
        adversary = reports.get("adversary")
        if adversary is not None and adversary.stance is Stance.BEARISH:
            base -= min(30.0, 6.0 * len(adversary.factors_against))

        # --- Evidence caps confidence -------------------------------------
        # Confidence has to be earned with observations, not with agreement.
        if sample == 0:
            base = min(base, 55.0)
        elif sample < MIN_MEANINGFUL_SAMPLE:
            base = min(base, 70.0)

        independent = len(by_family)
        if independent < 2:
            base = min(base, 50.0)

        confidence = max(0.0, min(100.0, base))

        reasons: list[str] = []
        risks: list[str] = []
        for report in reports.values():
            reasons.extend(f"[{report.agent}] {f}" for f in report.factors_for)
            risks.extend(f"[{report.agent}] {f}" for f in report.factors_against)

        risk_level = "HIGH"
        depth = by_family.get(SignalFamily.DEPTH)
        if depth is not None:
            risk_level = (
                "LOW" if depth.confidence >= 70
                else "MEDIUM" if depth.confidence >= 50 else "HIGH"
            )

        strategy = getattr(context, "proposed_strategy", "") or ""

        if veto is not None:
            decision, conviction, multiplier = (
                Decision.REJECT, Conviction.NONE, 0.0
            )
        elif confidence >= 70 and sample >= MIN_MEANINGFUL_SAMPLE:
            decision, conviction, multiplier = (
                Decision.PAPER_TRADE, Conviction.HIGH, 1.5
            )
        elif confidence >= 60:
            decision, conviction, multiplier = (
                Decision.PAPER_TRADE, Conviction.NORMAL, 1.0
            )
        elif confidence >= 52:
            decision, conviction, multiplier = (
                Decision.PAPER_TRADE, Conviction.SMALL, 0.6
            )
        elif confidence >= 45:
            decision, conviction, multiplier = (
                Decision.WATCH, Conviction.NONE, 0.0
            )
        else:
            decision, conviction, multiplier = (
                Decision.REJECT, Conviction.NONE, 0.0
            )

        # Thin books and violent tapes cut size regardless of enthusiasm.
        if risk_level == "HIGH" and multiplier > 0:
            multiplier = min(multiplier, 0.6)
            conviction = Conviction.SMALL
        if context.regime in ("high_volatility", "low_liquidity") and multiplier > 0:
            multiplier = min(multiplier, 0.6)

        # Correlation with what is already held.
        held = len(context.open_positions)
        same_strategy = sum(
            1 for p in context.open_positions
            if getattr(p, "strategy", "") == strategy
        )
        if same_strategy >= 3 and multiplier > 0:
            multiplier = min(multiplier, 0.6)
            risks.append(
                f"[portfolio] already holding {same_strategy} {strategy} "
                "positions: correlated exposure"
            )

        invalidation = [
            f"5m momentum turns negative from {s.price_change_5m:+.1%}",
            f"liquidity falls below ${s.liquidity_usd * 0.5:,.0f} (half of entry)",
            "buy/sell ratio drops under 1.0 on rising volume",
        ]
        if win_rate is not None:
            invalidation.append(
                "the matched-setup win rate falls below 40% as more close"
            )

        return PanelVerdict(
            symbol=s.symbol, decision=decision, confidence=round(confidence, 1),
            strategy=strategy, regime=context.regime, conviction=conviction,
            size_multiplier=round(multiplier, 2), risk=risk_level,
            historical_win_rate=win_rate, sample_size=sample,
            reasons=tuple(reasons[:6]), risks=tuple(risks[:6]),
            invalidation=tuple(invalidation), reports=reports,
            vetoed_by=(veto.agent if veto else ""),
        )
