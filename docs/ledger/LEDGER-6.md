# LEDGER-6: Section 10 reference exit rule

**Date:** 2026-09-23
**Class:** new code in the frozen area (`vpa.signal.exits`), implementing
Section 10. No threshold is invented: every number is read off the
specification, which declares itself "frozen permanently".

## What this is, and is not

Section 10 exists "only to make signals comparable... It is not a
trading strategy and must never be optimised."

Nothing in this module places an order or talks to a broker, and nothing
ever will (CLAUDE.md rule 1). It reads historical bars and answers one
question: treated the same way as every other signal, what would have
happened? The value is entirely in the sameness - candidates, controls
and both Section 11.2 baselines are measured by one rule.

| | |
|---|---|
| entry | the open of the next trading day after the signal |
| stop | 1.5 x daily ATR(20) adverse |
| target | 3.0 x daily ATR(20) |
| timeout | 10 trading days, exit at the close |
| forward moves | 1, 3, 5, 10 days, ATR-normalised, independent of the rule |

The target is exactly **+2R** and the stop **-1R** by construction,
which is what makes R-multiples comparable across stocks of different
volatility.

## Real-data proof: ANF, 13 January 2025

The Family A candidate from LEDGER-5 (slot 5 of the gap day):

```
entry 137.18 at the open of 2025-01-14, daily ATR 8.00
long : stop 125.19, target 161.17  ->  stop at 125.19 after 5 sessions, -1.00R
short: stop 149.17, target 113.19  ->  timeout at 120.82 after 10 sessions, +1.36R
forward moves from entry, in ATR: 1d -0.72  3d -1.14  5d -1.37  10d -2.05
```

The stock kept falling. Read long it is a full stop-out; read short it
is +1.36R at the timeout. **This is exactly why Section 10 calls Family
A direction-agnostic** - the same signal is -1R and +1.36R depending on
a direction the family does not claim to predict. Section 13.1 asks for
the *absolute* 5-day move, which here is 1.37 ATR.

## Readings

### 1. Family A is walked both ways

Section 10 gives Family B a short bias and calls Family A
"direction-agnostic", whose "primary metric is the **absolute**
ATR-normalised move". An exit rule needs a direction that an absolute
move does not have. Rather than invent one, Family A is walked long
**and** short and both outcomes are kept. Nothing downstream is forced
to choose, and Section 13.1 - the criterion this feeds - does not use
R-multiples at all.

### 2. A bar that reaches both levels is read as a stop

The pessimistic reading, applied identically every time. A measuring
stick may be wrong in a constant direction without much harm; what it
must not do is flatter some signals more than others, and an optimistic
reading would flatter the wildest bars most - exactly the bars these
patterns select for.

**Hourly bars are walked, not daily ones**, which removes most of these
cases: a day that touches both levels usually does so in different
hours. A test shows the same day resolving as a target when the hours
are ordered that way, and as a stop when they are not.

### 3. A gap through a level fills at the open

If a bar opened beyond the level, the level was gone before trading
began. The fill is the open. This cuts both ways - through the stop it
fills worse, through the target it fills better - so it is realism
rather than pessimism.

### 4. The daily ATR, and a trap that caught me

Section 10 says "daily ATR(20)". The stored **hourly** feature files
also carry a column called `atr20`, and it is a different quantity: the
ATR of the hourly bar sequence, which Section 5.2 uses for `spread_atr`.

On ANF's gap day the two were **7.996 and 3.719**. I passed the hourly
one into the first version of the real-data check, and the output looked
entirely plausible - stops and targets in the right places, an outcome
that read sensibly. It was only noticing that the printed ATR disagreed
with a figure measured earlier in the session that caught it.

The parameter is therefore named `daily_atr` at every call site, and the
module docstring says what goes wrong. **Section 7 is unaffected**, and
was checked: `spread_atr` correctly uses the hourly ATR per Section 5.2,
and every `dist_*` correctly uses the daily ATR per Section 5.6.

### 5. A delisting is not a timeout

A security acquired or delisted mid-window exits at its last close and
is recorded as `delisted`, with `resolved` false. Calling it a timeout
would record an acquisition as a flat result, and acquisitions are not
randomly distributed across candidates.

### 6. Forward moves are measured from the entry

"Independent of the exit rule" means they ignore the stop, the target
and the timeout - not that they are measured from somewhere else. They
run from the entry price, so a candidate and a Section 11.1 control
sampled on the same date are measured from the same kind of moment.

**The overnight move is deliberately excluded**, because Section 10's
entry is the next day's open and nobody can trade at yesterday's close.
On ANF that cost 1.43 of price - the stock gapped up overnight before
resuming its fall.

### 7. The timeout exits at the last bar of the tenth session

Ten sessions counted from the entry session inclusive, exiting at the
close. A test confirms the eleventh session is never read, which would
be look-ahead.

## How the rules were checked

Eight deliberate breaks, each confirmed to fail the tests:

| Break | Caught |
|---|---|
| Stop widened from 1.5 to 2.0 ATR | yes (10 tests) |
| Target cut from 3.0 to 2.0 ATR | yes (10) |
| Timeout extended from 10 to 15 sessions | yes (1) |
| The stop never checked at all | yes (4) |
| Gaps filled at the level rather than the open | yes (2) |
| A delisting recorded as a timeout | yes (2) |
| The window runs past the timeout | yes (1) |
| A horizon beyond the available data unguarded | yes (4) |

## Still open

- Applying this across the store, which belongs with Section 11.1's
  controls - the same machinery measures both.
