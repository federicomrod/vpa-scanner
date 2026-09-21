# Brief: building the Section 5 features

**Status:** proposal, for the project owner to approve or change.
**Scope:** Concept v2 Section 5 only - the numbers describing each bar.
No pattern detection (Section 7), no AI, no trading.

---

## 1. What this step produces

Every hourly and daily bar gains about 40 measurements, in four groups:

- **How busy was it?** Today's volume compared with the same hour on
  previous days (Section 5.1).
- **How far did it move, and what shape was the bar?** Range compared
  with the stock's usual range, where it opened and closed within that
  range, how long the wicks were (5.2, 5.3, 5.4).
- **Was that the stock, or the whole market?** The move with the market's
  move removed (5.5).
- **Where is the price, relative to landmarks?** Distance to recent highs
  and lows, to previous turning points, to price levels where a lot has
  traded, and to round numbers - all measured in units of the stock's own
  typical daily range (5.6, 5.7).

Every one of these is defined in Section 5 and **frozen**. This step
implements them; it does not choose or tune them.

---

## 2. The rule that matters most

**Every rolling window is strictly trailing and excludes the bar being
measured** (CLAUDE.md rule 5, Concept v2 Section 5).

If a feature for 10 March can see 11 March, a backtest will look
wonderful and mean nothing. This is the single most damaging bug
available to this project, and it is invisible at a glance: the numbers
look perfectly reasonable.

So the plan treats it as the main engineering problem, not a detail:

- **Every feature gets a look-ahead test**, from one shared helper. The
  test computes a feature, then changes the data *after* the bar being
  measured, recomputes, and fails if the answer moved by so much as a
  rounding error. A feature without this test is not finished.
- **Split adjustment is applied as of each bar's own date**, not today's.
  A split in 2024 must not change what a 2019 feature saw.
- Where a spec phrase could be read two ways, the brief below asks rather
  than guesses.

---

## 3. What I need decided before building (Class 2 - ledger entry)

Section 5 is frozen, so these are your calls, not mine. Each one changes
what counts as a signal.

**Q1. How is a percentile rank computed, exactly?**
The thresholds in Section 7 are "≥ 90", so ties matter. Proposal: the
percentage of trailing observations **strictly below** the current value.
A bar equalling the highest of 60 previous bars then scores 98.3, not
100. The alternative (counting ties as below) makes 100 reachable and
fires slightly more often.

**Q2. What happens to half days in the hourly baselines?**
A half day's last bar is a 30-minute stub with `slot_index` 3, while a
normal day's slot 3 is a full hour (12:30-13:30). Section 5.1 compares
"the same `slot_index`", which would put those two in the same pool and
compare 30 minutes of trading against 60. Proposal: **exclude half-day
bars from the baselines entirely**, and mark their own features null.
Roughly 2 days a year are affected. The alternative is to pool the stub
with slot 6, the other 30-minute bar.

**Q3. Which bars count as "valid observations"?**
Section 5.1 needs at least 40 valid observations out of 60. Proposal: a
bar is valid unless it is `low_quality` (fewer than 5 traded minutes) or
missing. An hour that genuinely traded nothing still counts as a real
observation of zero volume, since that is information, not a gap.

**Q4. Does the hourly market-relative feature use a daily beta?**
Section 5.5 defines `beta_60` from **daily** returns, but
`resid_ret_atr` is wanted on hourly bars too. Proposal: use the daily
beta, refreshed weekly as specified, applied to the hourly return of the
stock and of SPY.

**Q5. Sector features - defer or decide now?**
`sector_resid_ret_atr` and `sector_ret_day` need a sector classification,
which is Open Item 2 in Section 16 and still undecided. Proposal: build
everything else, leave these two columns null, and fill them in later
without recomputing anything else. The per-sector cap in Section 7.3 also
waits on this.

**Q6. The volume-at-price histogram (`dist_nearest_hvn`).**
Section 5.6 says 50 bins over the trailing 60 sessions but not over what
price span, nor from which bars. Proposal: bins spread evenly between the
lowest low and highest high of those 60 sessions, with each hourly bar's
volume placed at its own closing price. The alternative - spreading each
bar's volume across its high-low range - is defensible but slower and
adds a second choice to make.

**Q7. What is "the nearest level" for `level_touch_count` and
`level_age_bars`?**
Section 5.6 lists these two beside a list of candidate levels but does
not say which level they describe. Proposal: the nearest of the levels
already listed (prior day/week/month high and low, 20- and 60-day high
and low, nearest swing pivot, nearest high-volume node), measured in ATR
units; a "touch" is a bar whose high-low range contains the level.

**Q8. One ledger entry or several?**
Proposal: a single new entry, `LEDGER-2`, covering the first
implementation of Section 5, with these decisions recorded in it - the
same shape as LEDGER-1 for Sections 2 to 4.

---

## 4. How it will be built

Six pull requests, each reviewable on its own, in this order. Each is
small enough to read in one sitting and each ends with its features
computed, stored and tested.

| # | Contents | Why this order |
|---|---|---|
| 1 | Shared machinery: trailing-window helpers, percentile and robust-spread functions, the look-ahead test helper, feature storage | Everything else builds on it, and the anti-look-ahead test exists from the first line of feature code |
| 2 | Volume normalisation (5.1) | The core of the whole system; Section 7's main threshold uses it |
| 3 | Volatility, spread and candle geometry (5.2, 5.3) | Needed before anything can be expressed "in ATR units" |
| 4 | Price progress (5.4) | Depends on ATR from PR 3 |
| 5 | Market-relative (5.5), sector columns left null pending Q5 | Needs SPY, which is already downloaded |
| 6 | Structure and sequence (5.6, 5.7) | The largest; may split again if the swing-pivot and histogram work makes it unwieldy |

Features are stored under `~/vpa-data/derived/features/`, partitioned by
date like the bars, and are **rebuildable**: wrong feature code is fixed
by correcting it and rebuilding, never by patching stored numbers.

**Size:** about 20 million hourly bars and 3 million daily bars across
1,159 securities and ten years. Features are computed per security across
its whole history (which is how trailing windows work naturally), then
written out by date. I will measure the run time on one security before
running the lot, and report it before starting a long job, as with the
downloads.

---

## 5. How it will be checked

1. **Hand-worked examples.** For each feature, a small series whose
   expected answer is computed by hand in the test, not by the code being
   tested. This is what catches a wrong formula, which no amount of
   self-consistency checking will.
2. **The look-ahead test, on every feature** (Section 2 above).
3. **Boundary tests.** Too little history returns null rather than a
   number computed from a handful of bars; the "at least 40 of 60"
   condition is tested at 39 and 40.
4. **Property tests.** Percentiles stay within 0-100; the three candle
   fractions never exceed the bar; ATR is never negative.
5. **Deliberate breakage.** As with the data layer, I will break each
   important rule on purpose and confirm the tests catch it, and report
   what happened.
6. **Real-data sanity pass.** On a handful of known days (the
   reconciliation cases), check that features say what a human would say:
   the ANF gap day should show an extreme range and an extreme volume
   percentile; a quiet ITT day should show neither.

---

## 6. What could go wrong, and what is being done about it

| Risk | Handling |
|---|---|
| **Look-ahead** creeping in through a stray window | The shared test helper, applied to every feature; split adjustment as of each date |
| **Half days and stubs** silently polluting baselines | Q2 above, decided before code is written |
| **Thin stocks** producing meaningless percentiles | The 40-of-60 rule, plus the `low_quality` flag already on every bar |
| **Ties at the threshold** changing how often signals fire | Q1 above |
| **Sector data** still undecided | Q5: those two columns stay null and are added later without touching the rest |
| **Run time** on 20 million bars | Measured on one security first, reported before any long run |
| **Survivorship** in the early years | Already handled in the data layer, but worth re-checking once features exist: the 2017 lists changed by about 34 stocks a month after the share-count fix |

---

## 7. What this step does **not** do

- No pattern detection: Families A and B are Section 7, a separate step.
- No AI, no ranking, no report.
- No trading, ever, in any form.
- No changes to the frozen thresholds in Section 5. If one looks wrong
  while building, I will say so and stop; I will not adjust it.

---

## 8. What I need from you

1. Rulings on **Q1 to Q8** in Section 3. Q1, Q2 and Q3 block the first
   feature PR; the rest can follow.
2. Confirmation that **`LEDGER-2`** is the right home for them.
3. A decision on whether to run the **50-security top-up download** first,
   so features cover the whole rebuilt universe from the start. I would
   recommend doing that before PR 2 computes anything at scale.
