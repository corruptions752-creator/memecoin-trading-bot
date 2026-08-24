# Running it on your phone

Two ways. Pick by whether you can pay for hosting.

| | Free forever | Cadence | Setup |
|---|---|---|---|
| **GitHub Actions** | yes, no card | every ~15 min | all from a phone |
| **Replit / a VM** | needs paid always-on | every 30s | shell needed |

The cadence difference is real and matters — see the warning below.

---

# Option A — GitHub Actions (free, no credit card)

GitHub runs the bot on its own servers every 15 minutes, commits the results
back to the repo, and publishes the dashboard as a web page. Nothing to keep
running, nothing to pay.

## 1. Make the repo public

Repo → **Settings** → scroll to **Danger Zone** → **Change visibility** →
**Public**.

This is what makes it free: public repos get unlimited Actions minutes, private
ones get 2,000/month. There are no secrets in this code — no keys, no wallet,
no personal data — so public costs you nothing. (If you would rather keep it
private, edit `.github/workflows/paper-trade.yml` and change the cron to
`*/30 * * * *` to stay inside the free 2,000.)

## 2. Turn on Actions and Pages

- **Settings → Actions → General** → allow all actions.
- **Settings → Pages** → Source: **GitHub Actions**.

## 3. Start it

**Actions** tab → **Paper trade** → **Run workflow**. It then repeats every
15 minutes on its own.

## 4. Watch it

Your dashboard appears at:

```
https://corruptions752-creator.github.io/memecoin-trading-bot/
```

Add it to your home screen. It shows a **snapshot**, not a live feed — the
header tells you how old it is.

## The honest cost of free

GitHub's scheduler will not run anything faster than every 5 minutes, and
delays it further when busy. So the bot checks its stops every ~15 minutes
instead of every 30 seconds.

On meme coins that is a real handicap. A token can fall 50% between two checks,
and the bot will only find out at the next one. The workflow compensates by
widening the stop to 25% and holding for up to a day, because pretending a
15-minute loop is a 30-second loop would just produce wrong results.

**Read the results as a pessimistic floor.** If the strategy works at this
cadence, a faster one should do better. If it loses here, that does not prove
it would lose at 30 seconds — but it is not evidence to risk money on either.

---

# Option B — Replit (needs paid always-on for 24/7)

Free Replit sleeps when you close the tab, so the loop stops. Fine for watching
it work for an hour; not for three unbroken weeks.

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
