"""Contracts every analyst agent shares.

These agents are deterministic Python, not language-model calls. A cycle
runs inside a GitHub Actions job every few minutes with no API budget and
has to be reproducible from the ledger alone, so an "agent" here is a
specialist that reads the same context and returns a structured report.
That keeps the whole panel testable, free, and replayable after the fact.

The important field is `signal_family`. Five agents that all read the
5-minute candle are one opinion wearing five hats, and averaging them
manufactures confidence out of nothing. Reports carry the family of data
they drew on so the lead can weigh independent evidence instead of
counting votes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Stance(str, Enum):
    """What an agent concluded."""

    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    INSUFFICIENT_DATA = "insufficient_data"
    """The honest answer when an agent has no data to work with. It is a
    result, not a failure, and it must never be scored as neutral."""
    VETO = "veto"
    """Reserved for the risk agent: this trade must not happen."""


class SignalFamily(str, Enum):
    """The underlying data a report is derived from.

    Agents sharing a family are not independent witnesses.
    """

    PRICE_ACTION = "price_action"
    FLOW = "flow"
    """Buy/sell counts and volume -- who is trading, not what price did."""
    DEPTH = "depth"
    """Pool liquidity and what it costs to leave."""
    HISTORY = "history"
    """The bot's own closed trades."""
    SOCIAL = "social"
    META = "meta"
    """Reasoning about the other reports rather than about the market."""


@dataclass(frozen=True)
class AgentReport:
    """One specialist's structured opinion."""

    agent: str
    stance: Stance
    confidence: float
    """0-100. Must be evidence-based: see `evidence_n`."""
    signal_family: SignalFamily
    factors_for: tuple[str, ...] = ()
    factors_against: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    evidence_n: int | None = None
    """How many observations back this. None means the agent is reading the
    present rather than a sample. Zero means it has nothing."""
    setup: str = ""
    """Optional setup classification, e.g. "momentum breakout"."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 100.0:
            raise ValueError(
                f"{self.agent}: confidence {self.confidence} outside 0-100"
            )
        if self.stance is Stance.INSUFFICIENT_DATA and self.confidence > 0:
            raise ValueError(
                f"{self.agent}: insufficient data cannot carry confidence"
            )

    @property
    def directional(self) -> int:
        """+1 bullish, -1 bearish, 0 otherwise."""

        if self.stance is Stance.BULLISH:
            return 1
        if self.stance is Stance.BEARISH:
            return -1
        return 0


@dataclass
class AnalysisContext:
    """Everything the panel is allowed to look at for one candidate.

    Passed whole to every agent so each reads identical inputs -- a
    disagreement is then a real disagreement, not two agents looking at
    different numbers.
    """

    snapshot: object
    """The TokenSnapshot under consideration."""
    settings: object
    market: tuple = ()
    """Every snapshot seen this cycle, for breadth and regime."""
    history: tuple = ()
    """Closed trade records, oldest first."""
    open_positions: tuple = ()
    regime: str = "unknown"
    position_size_usd: float = 0.0
    reports: dict[str, AgentReport] = field(default_factory=dict)
    """Reports already filed this cycle. Agents that reason about other
    agents -- the adversary, the lead -- read this; the rest ignore it."""


class Agent(Protocol):
    """A specialist analyst."""

    name: str
    signal_family: SignalFamily

    def analyse(self, context: AnalysisContext) -> AgentReport:
        ...


def insufficient(
    agent: str, family: SignalFamily, why: str, evidence_n: int = 0
) -> AgentReport:
    """Build the honest no-answer report.

    Exists so that "I cannot tell" is one line to express and therefore
    never quietly rounded to neutral to keep a panel looking complete.
    """

    return AgentReport(
        agent=agent,
        stance=Stance.INSUFFICIENT_DATA,
        confidence=0.0,
        signal_family=family,
        notes=(why,),
        evidence_n=evidence_n,
    )
