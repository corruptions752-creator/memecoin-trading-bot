# Trading journal - run 1 (aggressive profile)

Closed out at reset. Kept because the record is the only thing that
tells the next run what actually happened, and a reset would erase it.

- Bankroll $1,000 -> realized **$-125.16** over 23 closed trades
- 4 winners / 19 losers (17.4% win rate)
- Average winner $50.97, average loser $-17.32 (payoff 2.94:1)
- Expectancy **$-5.44 per trade**; Kelly fraction negative
- Fees and slippage $28.03 ($1.22 per round trip)

## What the record says

The two trades that reached 3x made **+$164.75**. The other
21 lost **$-289.91**. This is a fat-tail strategy: 8.7% of
trades are the entire profit, so the job is to buy as many cheap shots at
that tail as possible and never cap one.

**Six positions ran up 59-132% and were still closed at a loss**, for
-$102.75 - 82% of the whole drawdown. The trailing stop was gated behind a
3x first-profit target, so between entry and 3x the only exit was the -35%
hard stop. Fixed by the give-back floor (breakeven past 1.5x, +25% past 2x).

**Entry score did not rank outcomes.** Split at the median of 13 scored
trades, the high half returned -$55.21 at avg peak 1.55x; the low half
-$52.64 at 1.73x. Being choosier was not the lever.

## Every closed trade

| token | cost | peak | return | P&L | exit |
|---|---:|---:|---:|---:|---|
| T&M | $50.70 | 1.08x | -44.5% | $-22.58 | stop_loss |
| $pigeon | $50.75 | 4.09x | +177.2% | $+89.93 | trailing_stop |
| もちまる | $54.24 | 1.38x | -42.4% | $-23.00 | stop_loss |
| BULLY | $50.86 | 1.28x | -38.7% | $-19.68 | stop_loss |
| FUBUKI | $52.91 | 1.59x | -36.6% | $-19.37 | stop_loss |
| TORQUE | $51.88 | 1.11x | -70.8% | $-36.70 | stop_loss |
| 1% | $50.76 | 2.32x | -36.2% | $-18.38 | stop_loss |
| csvoss | $50.93 | 3.21x | +146.9% | $+74.82 | trailing_stop |
| beluga | $50.77 | 1.46x | -13.5% | $-6.86 | time_stop |
| LARP | $50.76 | 1.05x | -16.6% | $-8.45 | time_stop |
| TILLY | $50.66 | 1.00x | -14.3% | $-7.26 | time_stop |
| CVXV666 | $49.60 | 1.19x | -7.7% | $-3.84 | time_stop |
| mNAV | $50.70 | 1.76x | -39.9% | $-20.25 | stop_loss |
| MrCate | $51.73 | 1.12x | -38.1% | $-19.73 | stop_loss |
| BATON | $50.54 | 1.00x | -47.0% | $-23.74 | stop_loss |
| X | $47.17 | 1.01x | -35.9% | $-16.94 | stop_loss |
| HAROLD | $49.24 | 1.97x | -25.1% | $-12.37 | time_stop |
| GTA6 | $50.60 | 1.07x | +5.0% | $+2.51 | time_stop |
| LEXUR | $50.25 | 1.08x | -36.9% | $-18.52 | stop_loss |
| APPLECAT | $45.74 | 1.34x | -41.5% | $-18.97 | stop_loss |
| BULLY | $45.75 | 1.89x | -45.2% | $-20.69 | stop_loss |
| SMOLCAT | $50.71 | 2.03x | -23.1% | $-11.69 | time_stop |
| YOURSELF | $49.52 | 2.43x | +74.0% | $+36.62 | time_stop |

## Deployment sweep run at reset (70 seeds, synthetic paths)

Testing "spend the whole bankroll", breaker off throughout:

| config | mean | median | best | winning seeds | mean w/o its best seed |
|---|---:|---:|---:|---:|---:|
| 4 slots x 25% (100% used) | $6.31 | -$253.82 | $5,020.97 | 22/70 | **-$66.36** |
| 5 slots x 15% (75% used) | $40.93 | -$99.19 | $2,899.02 | 28/70 | **-$0.49** |
| 3 slots x 5% (15% used) | $24.38 | -$10.10 | $606.90 | 34/70 | **+$15.94** |

The last column decides it. Remove each config's single luckiest seed and
both high-deployment variants go negative -- their whole mean is one seed.
Only the most concentrated, least-deployed config survives, and it also has
the best median, the most winning seeds and the smallest worst case.

Slot count moves the same way: 5 -> 8 -> 12 -> 20 slots ran +$30, -$2,
-$110, -$182 mean. More slots get filled with marginal candidates, and the
marginal candidate is where the negative expectancy lives.

Give-back floor, 30 seeds head to head on identical paths: mean delta
-$9.11 with a stdev of $111 -- indistinguishable from zero. It fixed six
real round-trips in run 1 but shows no measurable edge out of sample. Kept
because it closes a real structural hole, not because it is proven.
