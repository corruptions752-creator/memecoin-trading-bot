"""Command line entry point.

Usage::

    python -m memecoin_bot run      # start the trading loop
    python -m memecoin_bot scan     # screen the market once, trade nothing
    python -m memecoin_bot report   # show performance from the database
    python -m memecoin_bot close    # flatten every open position
    python -m memecoin_bot simulate # one offline run on synthetic prices
    python -m memecoin_bot sweep    # many runs; read the spread, not one run
    python -m memecoin_bot verify <mint>   # run every safety check on one token
    python -m memecoin_bot doctor   # hit the live APIs and show raw vs parsed
    python -m memecoin_bot dashboard  # web dashboard only, no trading
    python -m memecoin_bot run --once # one cycle then exit (CI schedulers)
    python -m memecoin_bot export     # write a dashboard snapshot to JSON
"""

import argparse
import logging
import sys
import time

from .broker import build_broker
from .config import PAPER, load_settings
from .engine import TradingEngine
from .market import DexScreenerClient
from .reporting import open_positions_table, summarize
from .risk import RiskManager
from .onchain import OnChainAuthorityProvider, apply_lp_policy
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
        choices=(
            "run", "scan", "report", "close", "simulate", "sweep", "verify",
            "doctor", "dashboard", "export",
        ),
        help="what to do",
    )
    parser.add_argument(
        "mint", nargs="?", help="token mint address (for the verify command)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--port", type=int, default=None,
        help="dashboard port (default: $PORT, else 8080)",
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="run the trading loop without serving the dashboard",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run a single trading cycle and exit (for scheduled runners)",
    )
    parser.add_argument(
        "--minutes", type=float, default=0.0,
        help=(
            "keep cycling for this many minutes, then exit. Lets one "
            "scheduled invocation do many cycles instead of one."
        ),
    )
    parser.add_argument(
        "--out", default="docs/state.json",
        help="where export writes the dashboard snapshot",
    )
    args = parser.parse_args(argv)

    # The offline commands print a report; per-trade logs would bury it.
    quiet = args.command in ("simulate", "sweep") and not args.verbose
    from .dashboard import default_port
    if args.port is None:
        args.port = default_port()

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
        from .config import resolve_lp_policy
        log.info("PAPER MODE — no real funds are at risk.")
        if resolve_lp_policy(settings) == "substitute":
            log.info(
                "LP-lock check relaxed to depth+age so paper trading can "
                "actually run; live mode enforces it strictly."
            )

    store = Store(settings.database_path)
    market = DexScreenerClient(settings)

    if args.command == "report":
        print(open_positions_table(store))
        print()
        print(summarize(store, settings.starting_bankroll_usd).render())
        return 0

    if args.command == "simulate":
        from .simulate import run_simulation
        print(run_simulation(settings))
        return 0

    if args.command == "sweep":
        from .simulate import run_sweep
        print(run_sweep(settings))
        return 0

    if args.command == "dashboard":
        from .dashboard import serve
        log.info("open http://localhost:%d in a browser", args.port)
        serve(settings, port=args.port)
        return 0

    if args.command == "doctor":
        from .doctor import run_doctor
        return run_doctor(settings, args.mint)

    if args.command == "verify":
        if not args.mint:
            log.error("usage: python -m memecoin_bot verify <mint address>")
            return 2
        return _verify(settings, market, args.mint, log)

    if args.command == "scan":
        return _scan(settings, market, log)

    import time

    # Restore saved bankroll and circuit-breaker state. Starting fresh while
    # reloading open positions would invent capital on every restart.
    risk = RiskManager.restore(settings, time.time(), store)
    engine = TradingEngine(
        settings, market, build_broker(settings), risk, store,
        OnChainAuthorityProvider(settings),
    )

    if args.command == "export":
        return _export(settings, store, args.out, log)

    if args.command == "close":
        closed = engine.close_all()
        log.info("closed %d position(s)", closed)
        return 0

    if args.once or args.minutes > 0:
        # A scheduled runner has no long-lived process, so the loop lives
        # inside one invocation. GitHub honours perhaps one scheduled firing
        # an hour however often it is asked, so the way to cycle often is to
        # cycle many times per firing rather than to beg for more firings.
        deadline = time.time() + args.minutes * 60.0
        cycles = 0
        while True:
            started = time.time()
            report = engine.run_cycle()
            cycles += 1
            log.info(
                "cycle %d: seen %d, shortlist %d, verified %d, "
                "entered %s, exited %s",
                cycles, report.scanned, report.shortlisted, report.candidates,
                report.entered or "none",
                [f"{s}:{r.value}" for s, r in report.exited] or "none",
            )
            # Export every cycle, so a job killed mid-run still leaves the
            # dashboard current rather than losing the whole window.
            _export(settings, store, args.out, log)

            if time.time() >= deadline:
                break
            elapsed = time.time() - started
            remaining = deadline - time.time()
            nap = max(0.0, min(settings.poll_seconds - elapsed, remaining))
            if nap > 0:
                time.sleep(nap)

        log.info("completed %d cycle(s)", cycles)
        return 0

    if not args.no_dashboard:
        from .dashboard import serve
        try:
            serve(settings, port=args.port, background=True)
            log.info(
                "dashboard live at http://localhost:%d — open it in a browser",
                args.port,
            )
        except OSError as error:
            # A busy port must not stop the bot from trading.
            log.warning(
                "dashboard could not start on port %d (%s); trading anyway",
                args.port, error,
            )

    engine.run_forever()
    return 0


def _export(settings, store, path: str, log) -> int:
    """Write the dashboard's state to a JSON file.

    A scheduled runner has no long-lived server, so the dashboard reads a
    committed snapshot instead of a live endpoint.
    """

    import json
    from pathlib import Path

    from .dashboard import build_state

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = build_state(settings, store)
    target.write_text(json.dumps(state, indent=1), encoding="utf-8")

    # The page ships alongside the snapshot so the pair can be served
    # statically by GitHub Pages with nothing else present.
    page = Path(__file__).parent / "dashboard.html"
    (target.parent / "index.html").write_text(
        page.read_text(encoding="utf-8"), encoding="utf-8"
    )
    log.info("wrote %s and %s", target, target.parent / "index.html")
    return 0


def _scan(settings, market, log) -> int:
    """Screen the live market once and print what would be traded."""

    candidates = market.discover()
    log.info("fetched %d candidate pair(s)", len(candidates))

    provider = OnChainAuthorityProvider(settings)
    passed = 0
    lp_only_blocks = 0

    for snapshot in candidates:
        # Cheap market checks first, so the on-chain calls are only spent on
        # candidates that could actually qualify. Public RPC rate limits.
        market_verdict = screen(snapshot, settings, require_contract_checks=False)
        if not market_verdict.passed:
            continue
        score, notes = score_entry(snapshot, settings)
        if score < settings.min_entry_score:
            continue

        authority = apply_lp_policy(
            provider.fetch(snapshot.mint), settings,
            liquidity_usd=snapshot.liquidity_usd,
            age_seconds=snapshot.age_seconds,
        )
        verdict = screen(snapshot, settings, authority)
        if not verdict.passed:
            if all("LP not confirmed" in r for r in verdict.failures):
                lp_only_blocks += 1
            log.debug("%s", verdict.describe())
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
    if lp_only_blocks:
        print(
            f"\n{lp_only_blocks} token(s) failed only the LP-lock check, which "
            "cannot be\nproven from pair data. Set MEMEBOT_LP_POLICY=substitute "
            "to accept pool\ndepth and age instead — a real loosening of "
            "safety, documented in the README."
        )
    return 0


def _verify(settings, market, mint: str, log) -> int:
    """Run every available safety check against one token and explain it."""

    snapshot = market.snapshot(mint)
    if snapshot is None:
        log.error("no market data for %s (bad address, or no live pair)", mint)
        return 1

    provider = OnChainAuthorityProvider(settings)
    authority = apply_lp_policy(
        provider.fetch(mint), settings,
        liquidity_usd=snapshot.liquidity_usd, age_seconds=snapshot.age_seconds,
    )
    verdict = screen(snapshot, settings, authority)
    score, notes = score_entry(snapshot, settings)

    print(f"\n{snapshot.symbol}  ({mint})")
    print("=" * 58)
    print(f"Price           : ${snapshot.price_usd:.10f}")
    print(f"Liquidity       : ${snapshot.liquidity_usd:,.0f}")
    print(f"24h volume      : ${snapshot.volume_24h_usd:,.0f}")
    print(f"Pair age        : {snapshot.age_seconds / 3_600:.1f}h")
    print()
    print("Contract checks:")
    for label, value in (
        ("mint authority revoked", authority.mint_authority_revoked),
        ("freeze authority revoked", authority.freeze_authority_revoked),
        ("LP locked or burned", authority.lp_locked_or_burned),
        ("sell simulation passed", authority.sell_simulation_ok),
    ):
        mark = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[value]
        print(f"  {label:<26} {mark}")
    holder = authority.top_holder_pct
    print(
        f"  {'top holder share':<26} "
        + (f"{holder:.1%}" if holder is not None else "UNKNOWN")
    )
    print()
    print(f"Entry score     : {score:.2f} ({', '.join(notes)})")
    print(f"VERDICT         : {'TRADABLE' if verdict.passed else 'REJECTED'}")
    if verdict.failures:
        for reason in verdict.failures:
            print(f"  - {reason}")
    print()
    print("UNKNOWN counts as a rejection. A check that could not be completed")
    print("is never treated as a pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
