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

- **A robust z-score with zero spread is null.** `vol_z_slot_60` divides
  by 1.4826 x MAD; when MAD is zero (at least half the window shares one
  value) the score is undefined, so the feature is null rather than
  infinite. Affects only near-dormant stocks, which the `low_quality`
  and 40-of-60 rules mostly exclude already.
