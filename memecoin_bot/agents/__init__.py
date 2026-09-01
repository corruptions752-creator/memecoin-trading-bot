"""The analyst panel: a roster of specialists and the lead that weighs them.

Adding an agent is appending to ROSTER. Nothing else in the system needs to
change: the context is passed whole, reports are collected by name, and the
lead groups by signal family rather than by a hard-coded list of agents.
"""

from __future__ import annotations

from .base import (
    AgentReport,
    AnalysisContext,
    SignalFamily,
    Stance,
    insufficient,
)
from .evidence import (
    AdversarialAgent,
    HistoricalPatternAgent,
    StatisticalValidationAgent,
    StrategyResearchAgent,
)
from .lead import Conviction, Decision, LeadTrader, PanelVerdict
from .market import (
    LiquidityRiskAgent,
    MarketScanner,
    MomentumAgent,
    SentimentAgent,
    TechnicalAnalyst,
)

# Order matters only in that agents reading the panel must come after the
# agents they read. The adversary is last for that reason.
ROSTER = (
    MarketScanner(),
    TechnicalAnalyst(),
    MomentumAgent(),
    LiquidityRiskAgent(),
    SentimentAgent(),
    HistoricalPatternAgent(),
    StrategyResearchAgent(),
    StatisticalValidationAgent(),
    AdversarialAgent(),
)


def run_panel(context: AnalysisContext, roster=ROSTER) -> PanelVerdict:
    """Run every agent in order, then the lead.

    Each report lands in ``context.reports`` as it is filed, so the later
    agents can reason about the earlier ones. A crashing agent must not
    take the cycle down with it -- it is recorded as having no opinion,
    which is the safe direction to fail in.
    """

    for agent in roster:
        try:
            context.reports[agent.name] = agent.analyse(context)
        except Exception as exc:  # noqa: BLE001 - one agent must not stop the panel
            context.reports[agent.name] = insufficient(
                agent.name, agent.signal_family, f"agent failed: {exc}",
            )
    return LeadTrader().decide(context)


__all__ = [
    "ROSTER", "run_panel", "AgentReport", "AnalysisContext", "SignalFamily",
    "Stance", "insufficient", "LeadTrader", "PanelVerdict", "Decision",
    "Conviction",
]
