# Trading journal - run 1 (aggressive profile)

Closed at reset, 107 trades. Kept because the record is the only
thing that tells the next run what happened.

- $1,000 -> realized **$-757.56**, ending with $30.07 cash
- 22 winners / 85 losers (**20.6%** win rate)
- Avg win $22.56, avg loss $-14.75 (payoff 1.53:1)
- Expectancy **$-7.08/trade**, Kelly -0.314

## Where the money went

| exit reason | n | total |
|---|---:|---:|
| stop_loss | 43 | $-695.11 |
| liquidity_collapse | 17 | $-426.22 |
| give_back | 8 | $-4.94 |
| time_stop | 35 | $65.52 |
| trailing_stop | 4 | $303.18 |

| peak reached | n | total | avg |
|---|---:|---:|---:|
| <1.2 | 62 | $-868.27 | $-14.00 |
| 1.2-1.5 | 19 | $-138.24 | $-7.28 |
| 1.5-2.0 | 14 | $-136.28 | $-9.73 |
| 2.0-3.0 | 7 | $82.42 | $11.77 |
| >=3.0 | 5 | $302.80 | $60.56 |

**62 of 107 trades never traded 20% above entry**, costing
$-868.27. That is the single biggest hole -- bigger than the
round-trips that the first 23 trades made look decisive. The 12 trades that
reached 2x made +$385.22 against
$-1,142.78 for everything else: still a fat-tail strategy.

`liquidity_collapse` cost $-426.22 over
17 exits -- invisible in the first 23 trades.

## What the sweeps said

Tested on synthetic paths, 50 seeds each, identical paths per seed. The
last column drops each config's single luckiest seed, which is what
separates a real effect from one lottery run:

| change | mean | median | worst | win seeds | w/o best seed |
|---|---:|---:|---:|---:|---:|
| baseline (stop 35%) | -$24.63 | -$76.10 | -$461.93 | 17/50 | -$44.13 |
| **stop 25%** | **-$0.48** | **-$40.82** | **-$380.95** | 20/50 | **-$12.00** |
| stop 20% | -$0.39 | -$43.30 | -$423.20 | 22/50 | -$14.87 |
| min liquidity $25k | +$1.68 | -$61.78 | -$461.93 | 19/50 | -$17.28 |
| min liq $25k + stop 20% | -$23.64 | -$50.15 | -$436.14 | 19/50 | -$36.61 |
| liquidity exit floor 70% | -$50.05 | -$96.62 | -$431.77 | 15/50 | -$66.68 |

Two things worth keeping:

**A tighter stop is the only robust improvement.** It survives the
leave-one-out check and wins on median and worst case. It works because it
attacks the 62-trade dead-on-arrival bucket directly.

**Exiting earlier on liquidity dips is worse, not better.** The obvious fix
for a $426 liquidity-collapse bill makes it worse, because it also cuts
positions that recover. The $25k minimum liquidity looked good alone and
reversed when combined with the tighter stop, so that was noise.
