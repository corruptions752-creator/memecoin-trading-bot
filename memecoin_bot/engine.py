"""The trading loop.

Order of operations in a cycle is deliberate: manage open positions *before*
looking for new ones. When the data feed is degraded or the bankroll is under
pressure, the bot's first duty is to the money already at risk.
"""

from dataclasses import dataclass, field, replace
import logging
import time

from .broker import Broker, ExecutionFailed
from .config import Settings
from .market import MarketData
from .models import (
    ExitReason,
    Position,
    Side,
    TokenSnapshot,
)
from .risk import RiskManager
from .onchain import apply_lp_policy
from .scanner import assess
from .safety import (
    AuthorityProvider, UnknownAuthorityProvider, categorize, screen,
)
from .store import Store
from .strategy import decide_entry, decide_exit

log = logging.getLogger(__name__)

#: How many tokens the scanner keeps. A cycle sees a few hundred; the
#: terminal shows the strongest reads rather than the whole feed.
SCANNER_CARD_LIMIT = 24


@dataclass(slots=True)
class CycleReport:
    """What one pass of the loop did, for logging and tests."""

    scanned: int = 0
    rejected: int = 0
    entered: list[str] = field(default_factory=list)
    exited: list[tuple[str, ExitReason]] = field(default_factory=list)
    failed_entries: list[str] = field(default_factory=list)
    failed_exits: list[tuple[str, ExitReason]] = field(default_factory=list)
    skipped_reason: str = ""
    rejections: dict[str, int] = field(default_factory=dict)
    """Why candidates were turned down, bucketed. Lets the dashboard explain
    a quiet scan instead of showing an empty screen."""
    candidates: int = 0
    """Passed every safety check and scored above the entry threshold."""
    shortlisted: int = 0
    """Cleared the free market checks and so were worth paying for on-chain
    verification. Contract checks run only on these."""
    rpc_failures: int = 0
    """On-chain lookups that could not read the chain at all. A rejection
    caused by an unreachable endpoint looks identical to one caused by a
    dangerous token, and they mean opposite things."""
    rpc_lookups: int = 0
    unverified: int = 0
    """Bought despite failing verification, under advisory mode."""
    near_misses: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    """Tokens that cleared the market checks and then failed verification,
    with every reason. A bucket count says how many; this says which."""
    assessments: list[dict] = field(default_factory=list)
    """Per-token reads for the scanner display, highest confidence first."""
    events: list[dict] = field(default_factory=list)
    """Things that happened this cycle, in order, for the activity feed."""


class TradingEngine:
    """Coordinates data, safety, strategy, risk, execution, and persistence."""

    def __init__(
        self,
        settings: Settings,
        market: MarketData,
        broker: Broker,
        risk: RiskManager,
        store: Store,
        authority: AuthorityProvider | None = None,
        *,
        enforce_contract_checks: bool = True,
    ) -> None:
        self.settings = settings
        self.market = market
        self.broker = broker
        self.risk = risk
        self.store = store
        self.authority = authority or UnknownAuthorityProvider()
        self.enforce_contract_checks = enforce_contract_checks
        self.positions: list[Position] = store.load_open_positions()
        self._held_snapshots: dict[str, TokenSnapshot] = {}
        # Seeded from the restored state so a restart mid-halt does not
        # announce a halt that has been in force for hours.
        self._was_halted = bool(getattr(risk, "halted", False))
        if self.positions:
            log.info("resumed %d open position(s)", len(self.positions))

    # --- Cycle ------------------------------------------------------------

    def run_cycle(self, now: float | None = None) -> CycleReport:
        """Manage open positions, then consider new entries once."""

        at = time.time() if now is None else now
        report = CycleReport()
        self._held_snapshots = {}

        if self.risk.roll_day(at):
            self.risk.persist(self.store, at)
        self._manage_positions(at, report)
        self._seek_entries(at, report)
        self._note_halt_change(report, at)
        self._record_activity(report, at)
        self._record_scan(report, at)
        self._flush_events(report, at)
        return report

    def run_forever(self) -> None:
        """Loop until interrupted, sleeping between cycles."""

        log.info(
            "starting in %s mode with $%.2f bankroll",
            self.settings.mode, self.risk.bankroll_usd,
        )
        try:
            while True:
                try:
                    self.run_cycle()
                except Exception:  # noqa: BLE001 - a cycle must never kill the loop
                    log.exception("cycle failed; continuing")
                time.sleep(self.settings.poll_seconds)
        except KeyboardInterrupt:
            log.info("interrupted; open positions remain recorded in the store")

    # --- Position management ----------------------------------------------

    def _manage_positions(self, at: float, report: CycleReport) -> None:
        """Apply exit rules to every open position."""

        for position in list(self.positions):
            snapshot = self.market.snapshot(position.mint)
            if snapshot is None:
                # No quote this cycle. Hold rather than dumping blind; the
                # time stop will still force the issue if it persists.
                log.warning("no snapshot for %s; holding", position.symbol)
                self._event(
                    report, "warn", at,
                    f"no quote for {position.symbol}; holding",
                    symbol=position.symbol, mint=position.mint,
                )
                continue
            self._held_snapshots[position.mint] = snapshot

            decision = decide_exit(position, snapshot, self.settings, at)
            if decision is None:
                self.store.update_position(position)
                continue

            self._execute_exit(position, snapshot, decision, at, report)

    def _execute_exit(self, position, snapshot, decision, at, report) -> None:
        """Sell part or all of a position and book the result."""

        quantity = position.quantity * min(1.0, max(0.0, decision.fraction))
        if quantity <= 0:
            return

        # A stop-loss or rug exit accepts a worse price rather than not
        # getting out; a profit-taking sell does not need to.
        urgent = decision.reason in (
            ExitReason.STOP_LOSS,
            ExitReason.LIQUIDITY_COLLAPSE,
            ExitReason.TRAILING_STOP,
        )

        # A full exit closes the token account, reclaiming the rent deposit.
        closing_out = decision.is_full_exit or quantity >= position.quantity

        try:
            fill = self.broker.sell(
                snapshot, quantity, reason=decision.reason.value,
                urgent=urgent, close_account=closing_out,
            )
        except ExecutionFailed as error:
            # The position is still open. Fees for a landed-but-reverted
            # transaction are gone regardless, and the exit is retried next
            # cycle -- which is exactly what happens live, and why a stop is
            # a target rather than a guarantee.
            self._charge_failed_transaction(error, at)
            report.failed_exits.append((position.symbol, decision.reason))
            self._event(
                report, "fail", at,
                f"sell of {position.symbol} failed "
                f"({error.result.failure.value if error.result.failure else '?'})"
                "; still holding",
                symbol=position.symbol, mint=position.mint,
                fee_usd=error.result.network_fee_usd,
            )
            log.warning(
                "exit for %s failed (%s); still holding %.4f units",
                position.symbol,
                error.result.failure.value if error.result.failure else "?",
                position.quantity,
            )
            return

        self.store.record_fill(fill, position.position_id)

        proceeds = fill.net_usd
        # Cost basis for the slice being sold, at the average entry price.
        cost_basis = position.entry_price_usd * quantity
        self.risk.record_sell(proceeds, cost_basis, at)
        self.risk.persist(self.store, at)

        position.quantity -= quantity
        position.realized_usd += proceeds - cost_basis

        if decision.reason is ExitReason.TAKE_PROFIT:
            position.took_first_profit = True

        closing = decision.is_full_exit or position.quantity <= 1e-12
        if closing:
            self._block_reentry(position.mint, decision.reason, at)
            self.store.close_position(position, decision.reason, at)
            self.positions.remove(position)
            report.exited.append((position.symbol, decision.reason))
            self._event(
                report, "sell", at,
                f"closed {position.symbol} on "
                f"{decision.reason.value.replace('_', ' ')} for "
                f"{'+' if position.realized_usd >= 0 else '-'}"
                f"${abs(position.realized_usd):,.2f}",
                symbol=position.symbol, mint=position.mint,
                realized_usd=position.realized_usd,
                reason=decision.reason.value, price=fill.price_usd,
                usd=proceeds,
            )
            log.info(
                "CLOSED %s (%s): realized $%.2f — %s",
                position.symbol, decision.reason.value,
                position.realized_usd, decision.note,
            )
        else:
            self.store.update_position(position)
            report.exited.append((position.symbol, decision.reason))
            self._event(
                report, "reduce", at,
                f"took profit on {position.symbol}: banked "
                f"${proceeds:,.2f}, {position.quantity:,.4f} units left",
                symbol=position.symbol, mint=position.mint,
                reason=decision.reason.value, price=fill.price_usd,
                usd=proceeds,
            )
            log.info(
                "REDUCED %s (%s): banked $%.2f, %.4f units left — %s",
                position.symbol, decision.reason.value, proceeds,
                position.quantity, decision.note,
            )

    def _verification_is_advisory(self) -> bool:
        """Whether contract findings advise rather than veto."""

        from .config import VERIFY_ADVISORY

        return getattr(self.settings, "verification", "") == VERIFY_ADVISORY

    def _fetch_authority(self, snapshot: TokenSnapshot):
        """Ask the provider about a token, passing the pool's own holding.

        Without it the pool counts as the top holder, and since a pool
        normally holds most of a meme coin's supply, every token with a
        healthy market is rejected as if it had a whale.
        """

        try:
            return self.authority.fetch(
                snapshot.mint, snapshot.pool_base_amount
            )
        except TypeError:
            # A provider that predates the parameter.
            return self.authority.fetch(snapshot.mint)

    @staticmethod
    def _count_rejection(report: CycleReport, failure: str) -> None:
        """Bucket one rejection for the dashboard."""

        bucket = categorize(failure)
        report.rejections[bucket] = report.rejections.get(bucket, 0) + 1

    def _record_activity(self, report: CycleReport, at: float) -> None:
        """Save what this scan saw, so a quiet market is explicable."""

        recorder = getattr(self.store, "save_activity", None)
        if recorder is None:
            return
        try:
            recorder(
                at=at,
                scanned=report.scanned,
                rejected=report.rejected,
                candidates=report.candidates,
                shortlisted=report.shortlisted,
                rpc_failures=report.rpc_failures,
                rpc_lookups=report.rpc_lookups,
                near_misses=report.near_misses,
                entered=len(report.entered),
                rejections=report.rejections,
                skipped_reason=report.skipped_reason,
            )
        except Exception:  # noqa: BLE001 - reporting must never stop trading
            log.debug("could not record activity", exc_info=True)

    def _event(
        self, report: CycleReport, kind: str, at: float, message: str, *,
        symbol: str = "", mint: str = "", **detail,
    ) -> None:
        """Note something that happened, for the activity feed.

        Every event corresponds to an action the bot actually took. The feed
        is a record, not decoration, so nothing is written here that did not
        happen.
        """

        report.events.append({
            "at": at, "kind": kind, "symbol": symbol, "mint": mint,
            "message": message, "detail": detail,
        })

    def _note_halt_change(self, report: CycleReport, at: float) -> None:
        """Log the circuit breaker only when it changes state."""

        halted = bool(getattr(self.risk, "halted", False))
        if halted and not self._was_halted:
            self._event(
                report, "halt", at,
                f"trading halted — {self.risk.halt_reason}",
                reason=self.risk.halt_reason,
            )
        elif self._was_halted and not halted:
            self._event(report, "resume", at, "trading resumed — limits reset")
        self._was_halted = halted

    def _flush_events(self, report: CycleReport, at: float) -> None:
        """Persist this cycle's events, heartbeat last."""

        self._event(
            report, "scan", at,
            f"scan complete — {report.scanned} seen, "
            f"{report.shortlisted} shortlisted, {report.candidates} tradable",
            scanned=report.scanned, shortlisted=report.shortlisted,
            candidates=report.candidates, entered=len(report.entered),
            rejected=report.rejected,
        )
        recorder = getattr(self.store, "record_events", None)
        if recorder is None:
            return
        try:
            recorder(report.events)
        except Exception:  # noqa: BLE001 - reporting must never stop trading
            log.debug("could not record events", exc_info=True)

    def _record_scan(self, report: CycleReport, at: float) -> None:
        """Persist the scanner's per-token reads."""

        recorder = getattr(self.store, "save_scan_tokens", None)
        if recorder is None:
            return
        try:
            recorder(report.assessments, at)
        except Exception:  # noqa: BLE001 - reporting must never stop trading
            log.debug("could not record scan tokens", exc_info=True)

    def _assess_held(self) -> list[dict]:
        """Scanner reads for the tokens the bot is currently holding."""

        out = []
        for position in self.positions:
            snapshot = self._held_snapshots.get(position.mint)
            if snapshot is None:
                continue
            out.append(assess(
                snapshot, self.settings,
                position_size_usd=position.cost_usd, held=True,
            ))
        return out

    def _charge_failed_transaction(
        self, error: ExecutionFailed, at: float
    ) -> None:
        """Deduct the fees burned by a transaction that did not execute.

        A transaction that lands and reverts still pays its fees. Ignoring
        that would make paper results better than live ones for no reason
        other than bookkeeping.
        """

        burned = error.result.network_fee_usd
        if burned <= 0:
            return
        self.risk.cash_usd -= burned
        self.risk.realized_today_usd -= burned
        self.risk.persist(self.store, at)

    def _block_reentry(self, mint: str, reason: ExitReason, at: float) -> None:
        """Bar a mint after exiting it.

        A drained pool earns a permanent ban; everything else earns a timed
        cooldown so the bot cannot stop out and immediately buy back in.
        """

        if (
            reason is ExitReason.LIQUIDITY_COLLAPSE
            and self.settings.ban_after_liquidity_collapse
        ):
            self.store.block_mint(
                mint, at, f"liquidity collapse at {at:.0f}",
                permanent=True, at=at,
            )
            log.info("permanently blocking %s after liquidity collapse", mint)
            return

        cooldown = self.settings.reentry_cooldown_seconds
        if cooldown <= 0:
            return
        self.store.block_mint(
            mint, at + cooldown, f"cooldown after {reason.value}", at=at
        )

    # --- Entries ----------------------------------------------------------

    def _seek_entries(self, at: float, report: CycleReport) -> None:
        """Scan, screen, score, and open at most the allowed positions."""

        allowed, why_not = self.risk.can_open(len(self.positions), at)
        if not allowed:
            report.skipped_reason = why_not
            log.debug("not entering: %s", why_not)

        candidates = self.market.discover()
        report.scanned = len(candidates)
        held = {position.mint for position in self.positions}
        blocked = self.store.blocked_mints(at)

        size_usd = self.risk.position_size_usd()
        shortlist: list = []
        ranked: list = []
        # Scanner reads for tokens that never make it past phase one. They
        # carry no on-chain data, and say so, rather than being dropped.
        watched: list[dict] = []
        for snapshot in candidates:
            if snapshot.mint in held or snapshot.mint == self.settings.quote_mint:
                continue
            if snapshot.mint in blocked:
                report.rejected += 1
                log.debug(
                    "skipping %s: %s", snapshot.symbol, blocked[snapshot.mint]
                )
                continue

            # Phase one: the market checks. These are local and free, so
            # they run on everything.
            market_verdict = screen(
                snapshot, self.settings, require_contract_checks=False
            )
            if not market_verdict.passed:
                report.rejected += 1
                self._count_rejection(report, market_verdict.failures[0])
                watched.append(assess(
                    snapshot, self.settings, verdict=market_verdict,
                    position_size_usd=size_usd,
                ))
                continue

            entry = decide_entry(snapshot, self.settings)
            if entry is None:
                report.rejected += 1
                report.rejections["low score"] = (
                    report.rejections.get("low score", 0) + 1
                )
                watched.append(assess(
                    snapshot, self.settings,
                    position_size_usd=size_usd,
                ))
                continue
            shortlist.append(entry)

        # Phase two: the contract checks. Each one costs several network
        # round trips against rate-limited endpoints, so they run only on
        # what survived phase one -- and only when there is room to act on
        # the answer. Checking all of them first turned a one-minute cycle
        # into a ten-minute one and left the dashboard permanently stale.
        verified: list[dict] = []
        for entry in shortlist:
            snapshot = entry.snapshot
            if not allowed:
                # No slot to fill, so no reason to spend a rate-limited
                # lookup. The card reports the contract checks as unchecked,
                # which is what they are.
                verified.append(assess(
                    snapshot, self.settings,
                    position_size_usd=size_usd,
                ))
                continue

            authority = apply_lp_policy(
                self._fetch_authority(snapshot), self.settings,
                liquidity_usd=snapshot.liquidity_usd,
                age_seconds=snapshot.age_seconds,
            )
            verdict = screen(
                snapshot, self.settings, authority,
                require_contract_checks=self.enforce_contract_checks,
            )
            verified.append(assess(
                snapshot, self.settings, authority, verdict,
                position_size_usd=size_usd,
            ))

            # Advisory verification: the findings are still computed and
            # still recorded against the position, they just do not veto.
            if not verdict.passed and self._verification_is_advisory():
                report.unverified += 1
                entry = replace(entry, unverified_reasons=verdict.failures)
                if len(report.near_misses) < 5:
                    report.near_misses.append(
                        (snapshot.symbol, verdict.failures)
                    )
                log.warning(
                    "ADVISORY: buying %s despite %s", snapshot.symbol,
                    "; ".join(verdict.failures),
                )
                self._event(
                    report, "advisory", at,
                    f"{snapshot.symbol} failed verification: "
                    + "; ".join(verdict.failures),
                    symbol=snapshot.symbol, mint=snapshot.mint,
                    failures=list(verdict.failures),
                )
                ranked.append(entry)
                continue

            if not verdict.passed:
                report.rejected += 1
                self._count_rejection(report, verdict.failures[0])
                if len(report.near_misses) < 5:
                    report.near_misses.append(
                        (snapshot.symbol, verdict.failures)
                    )
                log.info(
                    "near miss %s: %s", snapshot.symbol,
                    "; ".join(verdict.failures),
                )
                self._event(
                    report, "reject", at,
                    f"{snapshot.symbol} blocked: " + "; ".join(verdict.failures),
                    symbol=snapshot.symbol, mint=snapshot.mint,
                    failures=list(verdict.failures),
                )
                continue
            ranked.append(entry)

        report.shortlisted = len(shortlist)
        report.candidates = len(ranked)
        report.rpc_lookups = getattr(self.authority, "lookups", 0)
        report.rpc_failures = getattr(self.authority, "rpc_failures", 0)
        report.assessments = self._collect_assessments(verified, watched)

        if not allowed:
            return

        ranked.sort(key=lambda entry: entry.score, reverse=True)

        for entry in ranked:
            allowed, why_not = self.risk.can_open(len(self.positions), at)
            if not allowed:
                report.skipped_reason = why_not
                break
            self._execute_entry(entry, at, report)

    def _collect_assessments(
        self, verified: list[dict], watched: list[dict]
    ) -> list[dict]:
        """The scanner's card list: held tokens, then the strongest reads.

        Held positions are always present -- the bot's own money is never
        something the operator should have to go looking for -- and the rest
        of the room goes to the highest-scoring tokens seen this cycle.
        """

        cards = self._assess_held()
        seen = {card["mint"] for card in cards}

        rest = sorted(
            verified + watched,
            key=lambda a: (a["signal"] == "BUY", a["confidence"]),
            reverse=True,
        )
        for card in rest:
            if len(cards) >= SCANNER_CARD_LIMIT:
                break
            if card["mint"] in seen:
                continue
            seen.add(card["mint"])
            cards.append(card)
        return cards

    def _execute_entry(self, entry, at: float, report: CycleReport) -> None:
        """Open a position in the scored candidate."""

        snapshot: TokenSnapshot = entry.snapshot
        size_usd = self.risk.position_size_usd()

        try:
            fill = self.broker.buy(
                snapshot, size_usd, reason=f"score={entry.score:.2f}"
            )
        except ExecutionFailed as error:
            self._charge_failed_transaction(error, at)
            report.failed_entries.append(snapshot.symbol)
            self._event(
                report, "fail", at,
                f"buy of {snapshot.symbol} failed "
                f"({error.result.failure.value if error.result.failure else '?'})",
                symbol=snapshot.symbol, mint=snapshot.mint,
                fee_usd=error.result.network_fee_usd,
            )
            return

        if fill.quantity <= 0:
            log.warning("buy for %s returned no quantity", snapshot.symbol)
            return

        self.risk.record_buy(fill.net_usd)
        self.risk.persist(self.store, at)

        position = Position(
            mint=snapshot.mint,
            symbol=snapshot.symbol,
            entry_price_usd=fill.price_usd,
            quantity=fill.quantity,
            cost_usd=fill.net_usd,
            opened_at=at,
            entry_liquidity_usd=snapshot.liquidity_usd,
            peak_price_usd=fill.price_usd,
            unverified_reasons=entry.unverified_reasons,
        )
        position = self.store.open_position(position)
        self.store.record_fill(fill, position.position_id)
        self.positions.append(position)
        report.entered.append(snapshot.symbol)
        self._event(
            report, "buy", at,
            f"opened {snapshot.symbol} — ${fill.net_usd:,.2f} at "
            f"{fill.price_usd:.8f} (confidence {entry.score:.2f})",
            symbol=snapshot.symbol, mint=snapshot.mint,
            usd=fill.net_usd, price=fill.price_usd, score=entry.score,
            notes=list(entry.notes),
            unverified=list(entry.unverified_reasons),
        )

        log.info(
            "OPENED %s $%.2f @ %.10f (score %.2f: %s)",
            snapshot.symbol, fill.net_usd, fill.price_usd, entry.score,
            ", ".join(entry.notes),
        )

    # --- Manual controls --------------------------------------------------

    def close_all(self, reason: ExitReason = ExitReason.MANUAL) -> int:
        """Flatten every open position. Returns how many were closed."""

        closed = 0
        at = time.time()
        for position in list(self.positions):
            snapshot = self.market.snapshot(position.mint)
            if snapshot is None:
                log.warning("cannot price %s; leaving open", position.symbol)
                continue
            try:
                fill = self.broker.sell(
                    snapshot, position.quantity, reason=reason.value,
                    urgent=True, close_account=True,
                )
            except ExecutionFailed as error:
                self._charge_failed_transaction(error, at)
                log.warning(
                    "could not flatten %s (%s); still open", position.symbol,
                    error.result.failure.value if error.result.failure else "?",
                )
                continue
            self.store.record_fill(fill, position.position_id)
            self.risk.record_sell(
                fill.net_usd, position.entry_price_usd * position.quantity, at
            )
            position.realized_usd += (
                fill.net_usd - position.entry_price_usd * position.quantity
            )
            position.quantity = 0.0
            self._block_reentry(position.mint, reason, at)
            self.store.close_position(position, reason, at)
            self.positions.remove(position)
            closed += 1
        return closed

    def equity_usd(self) -> float:
        """Cash plus the marked value of open positions."""

        total = self.risk.cash_usd
        for position in self.positions:
            snapshot = self.market.snapshot(position.mint)
            price = snapshot.price_usd if snapshot else position.entry_price_usd
            total += position.value_usd(price)
        return total
