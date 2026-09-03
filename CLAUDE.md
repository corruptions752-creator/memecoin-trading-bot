# memecoin-trading-bot

Paper-trading meme coin bot. Runs a cycle every few minutes in GitHub
Actions, commits its ledger back to the repo, publishes a dashboard to Pages.
No real money is at risk anywhere in this repository.

## Standing direction: capital preservation outranks activity

Do not force trades. When no setup has a demonstrated edge the correct
output is **"NO TRADE — insufficient edge"** and cash. Size is the last
thing to raise while expectancy is negative.

`guarded` is the live profile: 3 slots x 5% (15% deployed), 5% daily
breaker, 25% stop, entry score 0.45, $25k minimum liquidity. Every number
was measured — across 70 seeds, 3 slots x 5% was the only configuration
whose mean survived dropping its luckiest seed.

`memecoin_bot/evidence_gate.py` sizes each playbook by its own live record.
A playbook that is negative over 20+ closed trades is throttled to 25% of
normal — throttled, not switched off, because cutting a thesis to zero
freezes its evidence at its worst moment and it can never earn its way
back. A positive record that goes negative without its single best trade
gets half size.

## Standing direction: run multiple win strategies

**The bot must pursue several independent winning strategies, not one.**
Every entry is attributed to the playbook that opened it (`Position.strategy`),
so results can be split per playbook and a loser can be retired on evidence
instead of opinion. When adding an idea, add it as a playbook with its own
entry rule and slot cap — do not widen the existing one until it stops
meaning something.

Playbooks live in `memecoin_bot/playbooks.py`. Each has a name, a scorer, a
threshold and a cap on how many of the open slots it may hold at once, so no
single thesis can take the whole book and no single regime failure wipes it.

## The analyst panel

Entries are reviewed by a panel of specialist agents in `memecoin_bot/agents/`.
They are deterministic Python, not model calls: a cycle runs in Actions every
few minutes with no API budget and has to be replayable from the ledger.

Adding an agent is appending to `ROSTER`. Nothing else changes — context is
passed whole, and the lead groups by signal family rather than by a list of
names.

Two mechanisms carry the design; do not remove them without replacing them:

- **Signal families.** Reports declare which data they read. The lead keeps
  the strongest voice per family and weighs families, rather than averaging
  agents — five agents reading the 5-minute candle are one opinion in five
  boxes. `HISTORY` outweighs the tape because it is the only family marked
  against reality.
- **Evidence caps confidence.** No comparable history caps the score at 55;
  under 30 matched setups caps it at 70; high conviction requires a real
  sample. Agreement never substitutes for observations.

`INSUFFICIENT DATA` is a first-class result and cannot carry confidence — the
report class raises if you try. The sentiment agent returns it every time on
purpose: no social feed is connected, and inferring sentiment from trade
counts would double-count the momentum agent.

Run `python -m memecoin_bot panel` for strategy, regime and agent-skill
tables built from closed setups.

### Agent skill is measured as lift, not accuracy

Score a bearish call against the overall win rate and a permanently bearish
agent looks like the best forecaster on the panel. It did exactly that here
on the first run: the adversary showed 76% accuracy and "+52% vs base" when
24% of trades won — it had simply said one thing every time. Lift is measured
per direction against that direction's own baseline, and the adversary
correctly drops to +0%. Never quote raw accuracy without it.

## Live results after the reset — the simulation did not transfer

45 closed trades, $1,000 -> **-$442.06**, 22.2% win, expectancy
**-$9.82/trade**. All three playbooks negative live:

| playbook | trades | live per trade | 60-seed sim said |
|---|---:|---:|---:|
| momentum | 30 | -$9.57 | -$2.30 |
| trend | 13 | **-$10.97** | **+$5.33** |
| reversal | 2 | -$6.19 | untested |

**`trend` was the sim's clear winner and is the worst performer live.** 13
trades is a small sample, but the direction is opposite. Treat every
synthetic-path ranking as a hypothesis to test live, never as a result.

**Panel confidence does not yet predict outcomes.** Splitting 23 scored
setups at the median: low-confidence half -$9.94/trade, high-confidence
half -$10.38. Correlation +0.148 at n=23 — indistinguishable from zero. The
panel is correctly refusing to be confident (range 52-61) but it is not
discriminating either, because it has no winning history to discriminate
with. Do not weight it as if it worked until this table says it does.

`liquidity_collapse` is again the second-biggest loss bucket: -$348 over 9
exits. Rug exposure at entry, not exit timing — raising the exit floor was
already tested and made things worse.

## Playbook results so far (60 seeds, synthetic paths)

| playbook | trades | win% | per trade | mean/seed | **w/o best seed** |
|---|---:|---:|---:|---:|---:|
| **trend** | 768 | 33% | **+$5.33** | +$68.22 | **+$62.28** |
| momentum | 1,945 | 23% | -$2.30 | -$74.55 | **-$85.08** |
| reversal | 0 | — | — | — | untested |

`trend` is the first positive-expectancy thesis measured in this repo and it
barely moves when its luckiest seed is dropped. `momentum` is the thesis that
lost $757 over 107 real trades, and the sweep agrees with the ledger. Shares
were set from this table: trend 0.5, momentum 0.375, reversal 0.25.

**momentum is demoted, not retired** — both real 3x winners came through it,
and the fat tail is where all the profit lives.

`reversal` never fired once in 60 seeds; the synthetic paths do not produce
dips with buy flow behind them. That is not evidence either way. It is
untested and the live feed has to rule on it.

Playbook shares deliberately sum above 1.0 so an idle thesis does not strand
slots in cash.

## What the record already proves — do not re-derive these

From run 1: 107 closed trades, $1,000 -> **-$757.56**, 20.6% win rate,
expectancy **-$7.08/trade**, Kelly **-0.314**. Full record in
`docs/journal/run-1-aggressive.md`. Read it before changing strategy.

- **It is a fat-tail strategy.** 12 trades reached 2x and made +$385; the
  other 95 lost $1,142. Never cap a runner to bank more small wins.
- **62 of 107 trades never traded 20% above entry**, costing $868. The
  dead-on-arrival bucket is the biggest hole, not the round-trips.
- **Entry score does not rank outcomes.** Split at the median, the
  high-score half did slightly worse. Being choosier on this score is not a
  lever.
- **Position size is not a lever while expectancy is negative.** Kelly is
  below zero, so scaling the bet multiplies a losing edge.
- **More slots is worse**, monotonically: 5/8/12/20 slots ran +$30, -$2,
  -$110, -$182 mean. Extra slots get filled with marginal candidates.

### Hypotheses already tested and killed

Do not retry these without new evidence; each cost a full sweep.

| tried | result |
|---|---|
| Deploy 75-100% of bankroll | Mean is one lucky seed. Drop each config's best seed and both high-deployment variants go negative. |
| Exit earlier on liquidity dips (floor .70/.85) | **Worse**, -$50 and -$107 mean vs -$25 baseline. It also cuts positions that recover. |
| Minimum liquidity $25k | Looked positive alone (+$1.68), reversed when combined with the tighter stop. Noise. |
| Give-back floor | Fixed six real round-trips in run 1, but 30 seeds head to head put it at -$9.11 +/- $111 — no measurable edge. Kept only because it closes a structural hole. |

## Method rules learned the hard way

- **Always run a leave-one-out check on a sweep.** At 30 seeds the
  100%-deployed config showed a +$289 mean; at 70 seeds with its best seed
  removed it was -$66. Report `mean without the best seed` alongside the mean.
- **An in-sample replay over past trades is optimistic.** The give-back floor
  looked like +$100 in replay and measured as noise out of sample.
- **A test that passes on the broken code is worthless.** After writing a
  regression test, break the fix and confirm the test fails, then restore.
  This has caught two useless guards in this repo already.
- The simulator's price paths are synthetic. Treat sweeps as evidence the
  rules fire and rank against each other, never as a forecast.

## Working on it

- `python -m pytest -q` — full suite, must stay green.
- `python -m memecoin_bot run --once` — one cycle. Market data is blocked
  from some sandboxes; it degrades to zero candidates rather than crashing.
- `python -m memecoin_bot simulate --minutes N` — offline synthetic run.
- Never commit a real private key. Paper mode needs no key at all.
