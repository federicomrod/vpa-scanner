# LEDGER-2: first implementation of Concept v2 Section 5 (features)

**Date opened:** 2026-09-21
**Class:** 2 (touches `src/vpa/signal/`)
**Type:** first implementation of the frozen specification, not a change
to it. No evaluation runs are spent - there is nothing yet to evaluate.

## What

Implement the ~40 per-bar measurements of Section 5. The thresholds and
formulas are frozen; this entry records only the readings needed where
the specification allows more than one, each approved by the project
owner on 2026-09-21 (see `docs/section-5-brief.md` for the brief that
asked them).

## Decisions

**1. Percentile ties - count ties as NOT below.**
A bar's percentile is `100 x (trailing values strictly below it) /
(valid observations)`. With 60 observations the highest possible score is
98.3, and clearing Section 7's "≥ 90" needs at least 54 of 60 below.
*Rejected:* counting ties as below, which reaches 100 and fires more
often at exactly the threshold the pattern families depend on.
*Owner:* "the conservative reading is right. Don't want ties inflating
candidate counts right at the threshold."

**2. Half days are excluded from the hourly baselines, and their own
bars get null volume and spread percentiles.**
A half day's last bar is a 30-minute stub sharing `slot_index` 3 with a
normal day's full 12:30-13:30 hour. *Rejected:* pooling the stub with
slot 6, the other 30-minute bar - about 2 half days a year gives ~20
observations in ten years, against a rule needing 40, so it would blend
a shortened pre-holiday session into the normal-day baseline. Half-day
bars are still stored and still shown in reports; they cannot fire.
Section 6 already flags half days as calendar events, which are excluded
from validation anyway.

**3. A trailing bar is a "valid observation" when it exists, is not
`low_quality`, and comes from a full session.**
Section 5.1 requires at least 40 valid observations out of 60; below
that the feature is null and the bar cannot be a candidate. This also
settles zero-volume bars: an hour with no trades has no traded minutes,
so it is `low_quality` and never enters a baseline.

**4. The hourly market-adjusted return uses the daily beta.**
`beta_60` (Section 5.5) is defined on daily returns and refreshed
weekly; it is applied to the hourly returns of the stock and of SPY. No
second beta is introduced. *Owner:* "use the daily beta, since that's
the only beta the spec defines. No new concept."

**5. Sector features stay empty for now.**
`sector_resid_ret_atr` and `sector_ret_day` are left null until the
sector classification source is chosen (Section 16, open item 2). That
same open item also blocks Section 7.3's per-sector cap. Filling them in
later requires no recomputation of anything else.

**6. Volume-at-price histogram (`dist_nearest_hvn`).**
Span: lowest low to highest high over the trailing 60 sessions,
split-adjusted as of the bar being measured, in 50 equal-width bins.
Source: the hourly regular-hours bars of those sessions (~420 bars).
Placement: each bar's whole volume goes to the bin containing its close.
The node is the heaviest bin; the feature is the distance from the
current close to that bin's centre, in daily ATR units.
*Rejected:* spreading each bar's volume across the bins its range covers
(adds a second arbitrary choice); using minute closes (~23,000 points
per window - more faithful, but re-reads minute data for every
security-day). *Owner:* "the minute-level version isn't worth the
runtime cost, and the high-low spread version just adds arbitrariness
for no real benefit."

**7. "Nearest level" means the nearest frozen pattern anchor - no new
levels.**
Eligible: 20-day high, 20-day low, prior-week high, prior-week low
(Family A's location test), prior-month high and nearest swing pivot
high (Family B's resistance references). `level_touch_count` and
`level_age_bars` describe whichever is nearest in ATR terms; a touch is
a bar whose high-low range contains the level.
Not eligible: prior-day high/low, 60-day high/low, high-volume nodes,
round numbers - their distances remain features in their own right, as
Section 5.6 lists, but they do not define "the nearest level".
The set is asymmetric (prior-month high and pivot high, no lows) because
Family B is short-biased and names only the highs. Inherited from the
frozen spec and deliberately not symmetrised. *Owner:* "leave it as-is.
Don't symmetrize it under this brief."

**8. All Section 5 decisions are recorded here**, in the same form as
LEDGER-1.

## Readings made during implementation (flagged for review)

### Section 5.1 (volume)

- **The "≥40 of 60" floor is carried across the other windows in the
  same two-thirds proportion:** 14 of 20, 80 of 120. Section 5.1 states
  the floor only for the 60-session window; requiring 40 everywhere
  would be impossible for a 20-session one.
- **A robust z-score with zero spread is null.** `vol_z_slot_60` divides
  by 1.4826 x MAD; when MAD is zero (at least half the window shares one
  value) the score is undefined, so the feature is null rather than
  infinite. Affects only near-dormant stocks, which the `low_quality`
  and 40-of-60 rules mostly exclude already.

### Sections 5.2 and 5.3 (volatility, spread, candle geometry)

- **The bar sequence is continuous across sessions.** The first hour of
  a day measures its true range from the previous day's closing hour, so
  an overnight gap counts as distance travelled. Without this a gap day
  looks like an ordinary quiet morning - on ANF's 13 January 2025
  guidance gap the opening hour's range is 29.64 with the gap and 16.46
  without it, which is 11x its usual range versus 6x.
- **Half-day bars keep `atr20` and `spread_atr`** but get a null
  `spread_pct_slot_60`. The first two are price measurements; only the
  percentile depends on a slot baseline, which decision 2 governs.
- **A bar with no trades has no prices**, so its true range is unknown,
  its features are null, and Wilder's running average carries across it
  unchanged rather than the gap poisoning every later value.
- **A bar that did not move has no shape.** Where the true range is zero
  (or, for `close_loc`, the high equals the low) the affected features
  are null rather than a division by zero.
- **`body_frac`, `upper_wick_frac` and `lower_wick_frac` are measured
  against the true range**, as Section 5.3 specifies, so on a gap bar
  they sum to less than 1 - the missing part is the gap itself.

### Section 5.4 (price progress)

- **"The trailing N bars" means this bar and the N-1 before it**, and
  the net move across them is measured from the close before that
  stretch began. So `progress_3` needs four closes, and is null until
  they exist.
- **A stretch counts only if every bar in it is a valid observation.**
  A three-bar volume total that includes a dead or half-day hour is
  neither compared against other stretches nor offered as one of them.
- **"Median slot volume" is the median over the same trailing 60
  sessions and slot as Section 5.1**, with the same validity rules. The
  0.1 floor that Section 5.4 specifies then does its job: without it a
  near-dormant stock would show enormous efficiency.
- **Half-day bars keep `ret_atr` and `progress_*`** but not
  `cum_vol_pct_*` or `efficiency`, which rest on volume baselines
  (decision 2).

### Section 5.5 (market-relative)

- **Units.** Section 5.5 writes `(ret - beta x ret_SPY) / atr20`, but a
  stock's move in dollars cannot be subtracted from the market's move in
  percent. SPY's return is converted into the stock's own money first -
  `beta x ret_SPY x previous close` - and the leftover dollars divided
  by `atr20`. This is the only dimensionally coherent reading, and it
  leaves `resid_ret_atr` on the same scale as `ret_atr`, which is what
  Section 7 compares (it applies the same ≤ 0.25 threshold to both).
- **"Refreshed weekly"** means fitted on the first trading day of each
  week and held for the rest of it. The fit uses returns through the
  previous session, so the value in force on a Monday knows nothing of
  that Monday. When a week starts on a holiday, the refresh happens on
  its first trading day.
- **A beta needs 40 of the 60 trailing daily returns**, the same floor
  as Section 5.1; below that `beta_60` and `resid_ret_atr` are null.
- **The daily beta is applied to hourly bars** (decision 4), with SPY's
  return taken over the matching hour.

### Section 5.6 (structure - levels)

- **Every distance is in daily ATR units**, hourly bars included.
  Section 7's location tests say "daily ATR" explicitly, so an hourly
  bar is measured against its session's daily ATR.
- **Distances are signed**: `(close - level) / daily ATR`, positive
  above the level. Section 7 compares the magnitude; the sign is kept
  because "just above the 20-day high" and "just below it" are
  different situations, and Section 5.6 asks for continuous distances
  rather than flags.
- **"Prior week" and "prior month" mean the last completed one**, never
  the one in progress; the 20- and 60-day extremes exclude the current
  session.

### Section 5.6 (structure - pivots and volume at price)

- **Pivots are found on daily bars** - they are chart-level landmarks,
  and Section 7 measures them in daily ATR.
- **A pivot exists only once the counter-move completes.** At the moment
  a high is made nobody knows it is a top, so the pivot dates from the
  session where price has fallen 1.5 x ATR from it, not from the extreme
  itself. A feature for a session uses only pivots confirmed before it.
- **The counter-move is measured against the daily ATR in force when the
  counter-move happens**, so the filter scales with the stock's own
  volatility, as Section 5.6's "scale-invariant" intends. A counter-move
  of exactly 1.5 x ATR confirms.
- **`dist_nearest_swing_pivot` is the distance to the nearer of the last
  confirmed pivot high and pivot low.** The pivot high is also kept
  separately, because Family B names it specifically.
- **The histogram** spans the trailing 60 sessions' lowest low to
  highest high in 50 equal bins, fed by hourly bars, each bar's whole
  volume going to the bin holding its close (decision 6). The node is
  the heaviest bin and the distance is to its centre.

### Section 5.6 (structure - the nearest level)

- **Age is counted in daily sessions** and dates from the session that
  **set** the level - the session that made the 20-day high, or made the
  prior week's high, or the swing pivot's own extreme.
- **A touch is a session whose high-low range contains the level**,
  counted over the trailing 60 sessions (the window Section 5.6 uses
  elsewhere); the session being measured is not counted.
- **The nearest level is chosen per bar**, so two hours of one session
  can be measured against different levels if price moved between them.

### Section 5.7 (sequence)

- **"The trailing 5 bars" excludes the bar being measured**, as
  everywhere else in Section 5. A bar's own busy-and-narrow shape is
  already in its own features; the count says what led up to it.
- **"The prior 10-bar high close" is the highest close among those ten
  bars**, not the close of whichever bar made the high.
- **These are hourly features.** Section 5.7 names `vol_pct_slot_60` and
  `spread_atr`, both hourly measures, and Section 7 applies them to
  hourly candidates.
- **Too little history gives "unknown", not "did not happen".** A failed
  new high needs ten bars behind it; with fewer, the flag is null rather
  than false.
