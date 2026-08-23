# Meme coin trading bot

A risk-managed Solana meme coin trading bot. It ships in **paper mode**: it
runs the full strategy against live market data and places simulated orders,
so the approach can be measured before any money is exposed.

## Read this first

Three claims are worth being blunt about, because most bots in this space are
sold on the opposite of them.

**There is no "perfect" entry or exit.** Selling the top requires knowing the
top in advance. What this bot does instead is execute a fixed ladder without
hesitation or hope: a hard stop, principal off at 2x, and a trailing stop on
the remainder. That is how you survive a category where most positions lose.

**"Low risk meme coin trading" is a contradiction.** Most new tokens go to
zero, and a meaningful share are rug pulls or honeypots. This bot can reduce
*execution* risk (screening, sizing, hard limits) but it cannot make the asset
class safe. Never fund it with money you need.

**Position sizing controls how fast you lose, not whether you win.** If the
strategy has no edge, risking 1% instead of 10% only changes how long the
decline takes. That is why paper mode is the default and the sweep exists.

Run `python -m memecoin_bot sweep` before believing anything.

## Commands

```bash
python -m memecoin_bot simulate   # one offline run on synthetic prices
python -m memecoin_bot sweep      # many runs; read the spread, not one run
python -m memecoin_bot scan       # screen the live market once, trade nothing
python -m memecoin_bot run        # trade + serve the dashboard on :8080
python -m memecoin_bot dashboard  # dashboard only, no trading
python -m memecoin_bot doctor     # hit the live APIs, show raw vs parsed
python -m memecoin_bot verify <mint>  # every safety check on one token
python -m memecoin_bot report     # performance and open positions
python -m memecoin_bot close      # flatten every open position
```

No dependencies to install. The bot uses only the standard library.

## Watching it trade

`run` serves a dashboard alongside the trading loop:

```bash
python -m memecoin_bot run
# open http://localhost:8080
```

It shows equity, today's P&L against the loss limit, open positions with each
one's progress between its stop and its target, closed trades with the reason
for every exit, and the recent fill log. It polls every five seconds and reads
straight from the bot's own database, so it cannot show anything the bot did
not actually do.

The page is one self-contained file with no dependencies, works in light and
dark, and lays out for a phone. Gains and losses carry a ▲/▼ glyph and a sign
as well as color, because the status green and red sit close together under
deuteranopia.

`--port` moves it; `--no-dashboard` runs the loop without it. On Replit the
web view gives the page a public URL, so it can be opened from a phone.

## Seeing the live data yourself

Do not take any number here on trust. `doctor` hits all three data sources for
real and prints the **raw API response next to the value the bot derived from
it**, so the parsing can be checked rather than believed:

```
  FIELD                         RAW FROM API          BOT READS AS
  priceUsd                        0.00002841         $0.0000284100
  liquidity.usd                   4812004.55            $4,812,005
  priceChange.m5                         1.8                +1.80%
  txns.m5.buys                           312                   312
```

It also decodes the mint account byte ranges in front of you, shows the actual
Jupiter sell quote, and ends with the bot's verdict and every reason behind it.

Run `python -m memecoin_bot doctor` any time a number looks wrong, or
`doctor <mint>` to point it at a specific token.

## Execution realism

Paper fills are not "the quoted price, instantly, for free". Every order runs
through a simulator that models what actually happens between a decision and a
confirmation:

| Effect | How it is modelled |
|---|---|
| Price impact | Real constant-product AMM math from inferred pool reserves |
| Latency | Log-normal, ~1.2s median — price moves while the tx is in flight |
| Adverse selection | In-flight drift is biased against you; the bot buys strength |
| Sandwich attacks | Probability scales with order size relative to the pool |
| Failed transactions | Slippage exceeded, dropped on congestion — fees still burn |
| Pool fee | 0.25%, the Raydium/Orca standard |
| Priority fee | Log-normal — spikes under congestion, when you most want to trade |
| Account rent | Deposited on first buy, refunded when the account is closed |

Two consequences worth knowing:

**A failed sell means you still hold the position.** The bot retries next
cycle. That is why a stop-loss is a *target*, not a guarantee — the same is
true live, and a simulator that pretends otherwise is lying to you.

**Constant-product math is not a flat percentage.** The difference is large
and runs both ways:

| Pool | Trade | Real impact | A flat 1.5% estimate |
|---|---|---|---|
| $25k | $1,000 | **8.25%** | 1.5% — 5x too optimistic |
| $1M | $10 | **0.25%** | 1.5% — 6x too pessimistic |

The thin-pool row is the dangerous one, and it is exactly where this strategy
trades.

## How a trade is decided

```
discover pairs
  -> safety screen      (disqualify: rug, honeypot, thin, wash traded, too new)
  -> entry score        (momentum, turnover, buy flow, depth)
  -> risk manager       (decides the size, or refuses)
  -> broker             (paper fill with fees and slippage charged)
  -> position store     (survives restarts)
```

Exits are re-evaluated every cycle, worst case first:

| Check | Default | Action |
|---|---|---|
| Liquidity collapse | pool below 50% of entry depth | close, ban the mint permanently |
| Stop loss | −15% from entry | close |
| Trailing stop | −25% from peak, after first target | close |
| First target | 2x entry | sell 50%, keep the runner |
| Time stop | 6 hours | close |

After any exit the mint is barred from re-entry for 6 hours. Without that, the
bot stops out at −15% and immediately re-buys the same falling token, turning
one bad trade into a grinding loop.

## Risk limits (conservative defaults)

| Setting | Default | Environment variable |
|---|---|---|
| Risk per trade | 1% of bankroll | `MEMEBOT_RISK_FRACTION` |
| Max open positions | 3 | `MEMEBOT_MAX_POSITIONS` |
| Stop loss | 15% | `MEMEBOT_STOP_LOSS_PCT` |
| First target | 2x | `MEMEBOT_TAKE_PROFIT_MULTIPLE` |
| Trailing stop | 25% | `MEMEBOT_TRAILING_STOP_PCT` |
| Max hold | 6h | `MEMEBOT_MAX_HOLD_SECONDS` |
| Daily loss limit | 5%, then halt | `MEMEBOT_DAILY_LOSS_LIMIT_PCT` |
| Re-entry cooldown | 6h | `MEMEBOT_REENTRY_COOLDOWN_SECONDS` |
| Min liquidity | $25,000 | `MEMEBOT_MIN_LIQUIDITY_USD` |
| SOL price (for fees) | $150 | `MEMEBOT_SOL_PRICE_USD` |
| Starting bankroll | $1,000 (paper) | `MEMEBOT_BANKROLL_USD` |

Sizing is a fixed fraction of the **whole bankroll**, not of free cash, so
holding three positions does not silently shrink the fourth. It shrinks after
losses and grows after wins automatically.

The daily loss limit halts new entries for the rest of the UTC day. Open
positions are still managed while halted — the breaker stops the bot digging,
it does not abandon what is already in the ground.

All of this state — bankroll, open positions, the halt, the day's realized
P&L, the re-entry blocklist — is persisted to SQLite and restored on startup.
A restart that re-read the starting bankroll while reloading open positions
would invent capital every time, which on a host that sleeps when idle is the
normal path rather than an edge case.

## On-chain safety checks

`OnChainAuthorityProvider` answers the contract-level questions by reading
Solana directly and asking Jupiter for a sell quote:

| Check | How it is answered | Catches |
|---|---|---|
| Mint authority revoked | SPL mint account, COption tag | Supply printed to zero under you |
| Freeze authority revoked | SPL mint account, COption tag | Your holdings frozen in place |
| Top holder share | `getTokenLargestAccounts` | A whale exiting into the pool |
| Sell simulation | Jupiter quote, token to SOL | **Honeypots** — the only check that proves you can exit |
| LP locked or burned | *not answerable from pair data* | Liquidity pulled outright |

Everything fails closed. A timeout, an RPC error, an unexpected account
layout, or a malformed response all produce "unknown", and the screen rejects
unknown. There is no path where a failed lookup becomes a favourable answer.

`verify` runs all of it against a single token and explains the verdict —
useful for checking something by hand before trusting the bot with it.

### The LP lock is honestly unknown

Proving LP is locked means identifying the specific pool, finding its LP mint,
and showing supply is burned or held by a known locker — and that differs per
DEX. It cannot be done honestly from pair data, so it is reported as `None`
rather than guessed. Two policies:

- `MEMEBOT_LP_POLICY=strict` (default) — unknown LP blocks the trade. Correct,
  and rejects most tokens.
- `MEMEBOT_LP_POLICY=substitute` — accepts pool depth ($100k+) and age (6h+)
  as a weaker proxy. **This is a real loosening of safety**, named explicitly
  so nobody enables it by accident.

Under `substitute`, the other four checks still apply in full — loosening the
LP rule never launders a failure elsewhere.

### Rate limits

The default public Solana RPC throttles aggressively. `scan` runs the cheap
market checks first and only spends RPC calls on candidates that could
qualify, and caches results for 15 minutes. A paid endpoint
(`MEMEBOT_RPC_ENDPOINT`) is required before live trading: a throttled safety
check fails closed, so the bot simply stops finding anything.

## What is deliberately not implemented

`LiveBroker` raises `NotImplementedError` rather than half-working. Finishing
it requires, at minimum:

1. Loading a wallet key from a secret (never a file in the repo), using a
   dedicated burner wallet funded only with what you will risk.
2. Fetching a Jupiter quote with an explicit `slippageBps`.
3. **Simulating the transaction before signing** — this is the honeypot check
   that actually proves you can sell.
4. Signing and sending with a priority fee, then confirming the signature.
5. Reconciling the actual filled amount against what was requested. On-chain
   fills routinely differ from quotes.

Live mode additionally refuses to start unless
`MEMEBOT_I_UNDERSTAND_THE_RISK=yes` is set.

## Testing

```bash
python -m pytest tests/ -q
```

269 tests covering the safety screen, sizing and the circuit breaker, the exit
ladder, AMM math, execution realism (failures, sandwiches, latency, rent),
state persistence across restarts, RPC rate limiting and retry, feed parsing
against malformed data, and end-to-end cycles including stop-outs, partial
profit taking, rugs, and re-entry blocking.

Engine tests run with stochastic execution effects switched off, so they test
decision logic rather than luck; execution realism is tested directly.
