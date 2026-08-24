# Running it on your phone

Three taps and about two minutes. Nothing to install.

## 1. Import into Replit

Go to [replit.com](https://replit.com) → **Create Repl** → **Import from GitHub**
→ paste:

```
https://github.com/corruptions752-creator/memecoin-trading-bot
```

## 2. Check it can reach the market

In the **Shell** tab:

```bash
python3 -m memecoin_bot doctor
```

You want three `[OK  ]` lines. If they fail, the bot has no data and nothing
below will work.

## 3. Press Run

That starts paper trading and the dashboard together. Replit opens a **Webview**
panel with a URL like `https://memecoin-trading-bot.<you>.repl.co`.

Open that URL on your phone. Add it to your home screen and it behaves like an
app.

## What you will see

The dashboard updates every five seconds:

- **Last scan** — how many pairs it looked at, how many cleared the filters,
  how many it bought, and the reasons it turned the rest down.
- **Equity, today's P&L, open slots, win rate.**
- **Open positions** with each one's progress between stop and target.
- **Closed trades** with the reason for every exit.
- **Fill log.**

## What is normal

**Most scans buy nothing.** The filters reject the large majority of tokens,
which is the point — the "Last scan" card shows you exactly why, so a quiet
screen is explicable rather than suspicious.

Expect a handful of trades a day, not a stream. If a whole day passes with
zero, check `doctor` first.

## The three-week plan

Let it run. Do not tune anything for the first week — changing the rules while
watching results is how people talk themselves into a strategy that does not
work.

After three weeks, read these numbers on the dashboard:

| Number | What it means |
|---|---|
| **Expectancy** | Dollars per trade. **Negative means the strategy loses.** |
| **Max drawdown** | Worst peak-to-trough fall. Could you sit through it? |
| **Win rate** | Low is fine if wins are much bigger than losses. |
| **Trades** | Under ~30, the numbers are noise. Keep going. |

Going live is a separate decision and a separate build. It is not implemented,
and it should not be until these numbers earn it.

## Free-tier limits

Replit's free plan sleeps the repl when idle, so the bot stops when you close
the tab. Open positions and the bankroll are saved to SQLite and resume on the
next start, but the loop is not running while it sleeps.

For a genuine unbroken three weeks you need Replit's paid always-on, or any
always-on machine. Worth deciding before you start counting the weeks.
