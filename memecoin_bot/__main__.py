"""Command line entry point.

Usage::

    python -m memecoin_bot run      # start the trading loop
    python -m memecoin_bot scan     # screen the market once, trade nothing
    python -m memecoin_bot report   # show performance from the database
    python -m memecoin_bot close    # flatten every open position
    python -m memecoin_bot simulate # one offline run on synthetic prices
    python -m memecoin_bot sweep    # many runs; read the spread, not one run
"""

import argparse
import logging
import sys

from .broker import build_broker
from .config import PAPER, load_settings
from .engine import TradingEngine
from .market import DexScreenerClient
from .reporting import open_positions_table, summarize
from .risk import RiskManager
from .safety import screen
from .store import Store
from .strategy import score_entry


def _configure_logging(verbose: bool) -> None:
    """Set up console logging."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a command."""

    parser = argparse.ArgumentParser(prog="memecoin_bot")
    parser.add_argument(
        "command",
        choices=("run", "scan", "report", "close", "simulate", "sweep"),
        help="what to do",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    # The offline commands print a report; per-trade logs would bury it.
    quiet = args.command in ("simulate", "sweep") and not args.verbose
    _configure_logging(args.verbose)
    if quiet:
        logging.getLogger("memecoin_bot").setLevel(logging.WARNING)
    log = logging.getLogger("memecoin_bot")

    try:
        settings = load_settings()
    except RuntimeError as error:
        log.error("%s", error)
        return 2

    if settings.mode == PAPER:
        log.info("PAPER MODE — no real funds are at risk.")

    store = Store(settings.database_path)
    market = DexScreenerClient(settings)

    if args.command == "report":
        print(open_positions_table(store))
        print()
        print(summarize(store).render())
        return 0

    if args.command == "simulate":
        from .simulate import run_simulation
        print(run_simulation(settings))
        return 0

    if args.command == "sweep":
        from .simulate import run_sweep
        print(run_sweep(settings))
        return 0

    if args.command == "scan":
        return _scan(settings, market, log)

    import time

    risk = RiskManager.start(settings, time.time())
    engine = TradingEngine(
        settings, market, build_broker(settings), risk, store,
        # Contract checks require an on-chain provider that is not wired in
        # yet. Paper mode runs the market screen and reports the contract
        # findings as warnings; live mode always enforces them.
        enforce_contract_checks=settings.mode != PAPER,
    )

    if args.command == "close":
        closed = engine.close_all()
        log.info("closed %d position(s)", closed)
        return 0

    engine.run_forever()
    return 0


def _scan(settings, market, log) -> int:
    """Screen the live market once and print what would be traded."""

    candidates = market.discover()
    log.info("fetched %d candidate pair(s)", len(candidates))

    passed = 0
    for snapshot in candidates:
        verdict = screen(snapshot, settings, require_contract_checks=False)
        if not verdict.passed:
            continue
        score, notes = score_entry(snapshot, settings)
        if score < settings.min_entry_score:
            continue
        passed += 1
        print(
            f"{snapshot.symbol:<12} score={score:.2f}  "
            f"liq=${snapshot.liquidity_usd:>12,.0f}  "
            f"5m={snapshot.price_change_5m:>7.1%}  "
            f"{', '.join(notes)}"
        )

    if not passed:
        print("Nothing passed the screen. That is a normal result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
