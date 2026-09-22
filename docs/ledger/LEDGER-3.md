# LEDGER-3: measured candidate yield at the frozen Section 7 thresholds

**Date:** 2026-09-22
**Class:** measurement only. No code, no thresholds and no rules change.
**Answers:** Section 18's reviewer question 1 - "Are the thresholds in
Section 7 permissive enough? Estimate the actual candidate yield against
real data before freezing. If it fires 5 times a day or 500, the numbers
need revisiting **now**, not after validation begins."

## What was measured

Section 5's features, computed over the whole ten-year store, with
Section 7's filters applied as written. This is an analysis run, not
committed detector code: Section 7 is implemented separately, and these
figures were produced to inform that work, not by it.

- 242 sessions sampled evenly across 2016-10 to 2026-09 (every tenth).
- Only universe members trading that day, `low_quality` bars excluded.
- **Before** Section 6's event exclusions, which do not exist yet.
- Family B's resistance test used the nearest swing pivot rather than
  specifically a pivot high, so its count is slightly generous.

## The answer: the thresholds hold

| | per day |
|---|---|
| Family A | mean 24.8, median 29 (58-day cut: 29.2) |
| Family B | mean 4.4 |
| Both | median 31, 90th percentile 56 |

Section 7.3 expects "roughly 30-60 candidates per day" with a hard cap
of 60. Observed median is **31**; the cap was exceeded on **2 of 58**
days (worst 72). It fired on every day sampled - never zero. The rate is
stable by year, from 27 a day in 2022 to 37 in 2021.

**No threshold needs revisiting.** It is neither firing 5 times a day
nor 500.

## The asymmetry worth recording

Family A produces about **six times** as many candidates as Family B
(24.8 against 4.4 a day). Section 13.3 requires ≥250 date-clustered
effective occurrences **per family**, so B is the binding one - but by
day rather than by candidate:

- Family A fires on **98%** of sessions, ~20 distinct stocks when it does.
- Family B fires on **93%** of sessions, ~4.6 distinct stocks when it does.

Clustered by date, B therefore yields roughly **234 effective
occurrences a year**, not 4 a day.

## Is Family B the long pole? No.

Against the ten-year store, after removing feature warm-up (~120
sessions) and the 12-month holdout (Section 11.4), about 2,130 sessions
remain for development. At 93%, that is ~1,980 days with a Family B
candidate.

Section 6's exclusions will reduce that, and the reduction is smaller
than it first appears, because the two kinds of flag behave differently:

- **Calendar and macro flags** (option expiry, triple witching,
  month-end, quarter-end, half days, the day after a holiday, FOMC, CPI,
  non-farm payrolls) remove whole days: roughly 27% of sessions.
- **Company flags** (earnings D-1/D0/D+1, ex-dividend, split, index
  changes, halts) remove individual candidates, not the day - another
  stock's candidate on the same day survives.

So expect on the order of **1,400 effective Family B occurrences**
available historically, against a requirement of 250. Comfortable.

**The contrast worth knowing:** if Family B had to be validated on
*new* data only, the same rates give ~170 effective occurrences a year,
so 250 would take about **18 months**. It is the historical store that
makes B tractable, which is a reason to protect it.

**The real long pole is the blind labels** (Section 12): 300+ at 10
charts a session is ~30 labelling sessions over roughly four months of
the trader's time, plus a 28-day gap before the test-retest subset. That
is calendar-bound and cannot be parallelised or bought.

## Caveats

- Before Section 6's exclusions; the ~27% day-level figure is derived
  from the calendar, not measured, and should be re-measured once the
  flags exist.
- Family B's resistance test is slightly generous (see above).
- 242 of 2,504 sessions sampled, not the whole store.
