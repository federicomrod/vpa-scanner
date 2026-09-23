# LEDGER-7: Section 11.1 matched controls

**Date:** 2026-09-23
**Class:** new code in the frozen area (`vpa.signal.controls`),
implementing Section 11.1's control sampling.

## Why the controls decide whether Section 13.1 means anything

Candidates are not a random slice of the universe. By construction they
are its busiest bars, on stocks near a level. Comparing their forward
moves against the universe average would mostly measure that selection,
not the pattern.

Section 11.1's controls ask the question that matters: **did the pattern
add anything beyond being a busy, liquid stock on that day?** Five
ticker-days, same date, same universe snapshot, matched on volatility
and liquidity, which did not trip the filter.

## Reading 1: "ATR-percentile" means volatility, not price

Section 11.1 says "matched on ATR-percentile decile". It does not say
percentile of what, and the two readings are not close.

**Implemented: ATR as a fraction of price.** Measured over 25 sampled
dates:

| | |
|---|---|
| rank correlation, raw ATR vs share price | **+0.83** |
| stocks in the same decile under both readings | **13%** |
| average decile shift when they differ | **2.8** |

Raw ATR is very largely a proxy for **share price** - a $500 stock has a
bigger ATR than a $50 one at identical volatility - so matching on it
would pair candidates with stocks of similar price rather than similar
volatility. Since every metric downstream is already ATR-normalised, the
purpose of matching on ATR is to hold the *volatility regime* constant,
and relative ATR is what does that.

Only 13% of stocks land in the same decile either way, so this is not a
detail: it changes almost every control set in the study.

## Reading 2: five controls do not fit in a decile cell

A 400-stock universe spread over 10 x 10 decile cells holds **four
stocks per cell on average**. Section 11.1 asks for five. Measured over
25 sampled dates:

| | |
|---|---|
| universe members | ~393 |
| decile cells populated | 97 of 100 |
| mean cell occupancy | **4.1** (median 4, p90 7, max 18) |
| cells holding 5 or more | **36%** |
| share of the universe in such a cell | **55%** |

The two axes are nearly independent - rank correlation +0.13 - so the
cells fill fairly evenly. This is arithmetic, not a quirk of the data:
400 over 100 cells is 4.

**The match is therefore relaxed by one decile on each axis when a cell
is too thin**, turning one cell into a 3 x 3 block of roughly forty
stocks. That is still a close match, and it is enough every time.

**How far each match was relaxed is recorded per control and reported.**
"Matched controls" quietly meaning "matched within one decile" for
roughly half the sample is exactly the sort of thing that should be
visible in the output rather than buried in a module.

Alternatives considered: quintiles instead of deciles (25 cells, ~16
each, exact matching throughout) would depart from the specification's
wording; matching one axis exactly and the other loosely would be
arbitrary about which. Widening is the smallest honest change, and it is
measurable.

## Reading 3: the draw is deterministic, and keyed to the candidate

Section 7.3 insists a capped candidate set be reproducible. A control
set has to be too, or no two runs of the analysis can be compared.

Controls are ordered by `sha1(date | candidate | slot | control)` and
the first five taken. Consequences, each with a test:

- The same candidate always draws the same five, whatever order the rows
  arrive in.
- **Two candidates in the same cell on the same day draw different
  fives.** Keying only on the date would give every candidate in a cell
  one shared control set, making the comparison far less independent
  than it looks.
- Two bars of the same stock on the same day are two candidates and draw
  separately.

## Reading 4: dollar volume comes from the universe snapshot

Section 2's own measure - the trailing 60-day median - already stored on
each monthly snapshot, already point-in-time, and constant within a
month so the decile cells do not shift underneath the matching.

## How the rules were checked

Nine deliberate breaks, each confirmed to fail the tests:

| Break | Caught |
|---|---|
| Three controls per candidate instead of five | yes (3 tests) |
| Matched on raw ATR rather than relative | yes (2) |
| A candidate allowed to be its own control | yes (1) |
| The match never widens | yes (7) |
| Every candidate in a cell draws the same controls | yes (2) |
| Two bars of one stock draw the same controls | yes (1) |
| Deciles computed without tie-breaking | yes (1) |
| Widening never recorded | yes (3) |
| Controls taken in row order rather than hashed | yes (3) |

## Still open

- Running this across the store, together with Section 10's outcomes -
  the two make Section 13.1 computable.
