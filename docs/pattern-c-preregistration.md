# Pattern C: pre-registration

**Written 2026-09-23, before any test against history.** Nothing in this
document has been evaluated against forward returns. The point of
writing it first is that it cannot be adjusted afterwards without that
being visible in the git history.

**This is variant 2** for Section 11.4's false-discovery correction.
Variant 1 was LEDGER-9's evaluation of Families A and B.

## What Pattern C is for

Families A and B each look at **one bar**. A discretionary Volume Price
Analysis read rarely does: it looks at a stretch of bars and asks
whether repeated effort is failing to move price, and then which way the
stretch resolves.

Family A is that idea compressed into a single bar, which throws away
the repetition. Family B is a single-bar rejection, bearish only.
Pattern C is the multi-bar version, and it is **symmetric** - it fires
on absorption at support as readily as distribution at resistance.

## The definition

An hourly bar is a **Candidate-C** if all of the following hold.

| # | condition | why |
|---|---|---|
| 1 | `repeat_hv_narrow_5 >= 2` | at least two of the previous five bars were busy **and** narrow - the repetition, and the thing that makes C not A |
| 2 | `cum_vol_pct_5 >= 90` | the five-bar stretch's total volume is in its own top decile - sustained effort, not one spike |
| 3 | `abs(progress_5) <= 0.5` | net movement across the stretch is under half a daily ATR - the effort produced no result |
| 4 | `vol_pct_slot_60 >= 90` | this bar is busy too, the same gate A and B use |
| 5 | `low_quality` is false | Section 4.3 |
| 6 | location within **1.5 x daily ATR** of a level | the same limit as Family A |
| 7 | a **direction**, below | C is symmetric; the direction is assigned, not guessed |

### Condition 7, the direction

- **C-up (bullish)**: `close_loc >= 0.60` and the nearest level within
  1.5 ATR is a **low** (`dist_low_20` or `dist_prior_week_low`).
  Repeated heavy trading at support that will not break, closing strong.
- **C-down (bearish)**: `close_loc <= 0.40` and the nearest level within
  1.5 ATR is a **high** (`dist_high_20` or `dist_prior_week_high`).
  Repeated heavy trading at resistance that will not clear, closing weak.
- A bar meeting neither is **not** a Candidate-C. The middle of the
  range is not a read.

### Deliberately not used

- **`efficiency`** - conditions 2 and 3 already say "much trading, no
  movement" over the stretch, and more usefully. Adding it would narrow
  the filter without adding an idea.
- **`repeat_upper_reject_5`** - that is Family B's shape. Using it here
  would blur the two. It is passed forward as a **feature**, as Section
  7.1 does with the repeat counts.
- **Event flags** - same as A and B: validation excludes flagged days,
  production marks and keeps them. Not part of what makes a candidate.

## Where the numbers come from

Every threshold is either **copied from the frozen families** or set by
**yield calibration**, and never from looking at what happened next.

| threshold | source |
|---|---|
| `vol_pct_slot_60 >= 90` | Sections 7.1 and 7.2, unchanged |
| location `<= 1.5` ATR | Section 7.1, unchanged |
| `cum_vol_pct_5 >= 90` | the same top-decile idea as condition 4 |
| `close_loc` 0.60 / 0.40 | Section 7.2 uses 0.35 for "weak"; 0.40/0.60 is its symmetric widening, since C has no wick requirement to lean on |
| `abs(progress_5) <= 0.5` | Section 7.1 allows 0.25 ATR of movement in one bar; half an ATR over five is the same tolerance, slightly tightened per bar |
| `repeat_hv_narrow_5 >= 2` | **calibration** - see below |

`repeat_hv_narrow_5 >= 1` occurs on 8.1% of bars, `>= 2` on 1.82%,
`>= 3` on 0.48%. Combined with conditions 2 and 3:

| repeat | cum_vol | bars | candidates/day |
|---|---|---|---|
| >= 1 | >= 90 | 1.18% | ~32 |
| **>= 2** | **>= 90** | **0.49%** | **~13** |
| >= 2 | >= 80 | 0.56% | ~15 |

`>= 2` was chosen because it lands inside Section 7.3's "roughly 30-60
candidates per day" once C is added to A and B, and because "two of the
last five" is the weakest claim that is still repetition. `>= 3` would
leave about 3 a day, too few to reach Section 13.3's 250 occurrences in
reasonable time.

**Measured before location and direction filtering**, on 19 sessions
sampled across 2018-2026. Expect meaningfully fewer after conditions 6
and 7 - likely 5 to 8 a day, which is the right order next to Family
B's 4.

**This calibration looked only at how often the filter fires.** No
forward return, no exit outcome, no control comparison was computed for
any variant of Pattern C, including the ones in the table above.

## How it will be judged

Exactly as A and B were, with nothing relaxed:

- Section 10's reference exit rule, unchanged.
- Section 11.1's five matched controls per candidate.
- Section 13.1: edge `>= 0.15` ATR over matched controls on the mean
  absolute 5-day move, `p < 0.01` date-clustered, holding in `>= 2 of 3`
  folds **on the strict reading** - the whole criterion inside each
  fold, as decided for Family B.
- Section 13.3's gate first: 250 date-clustered effective occurrences,
  three calendar years, one 15% SPY drawdown.
- The holdout is not touched.

C-up and C-down are **evaluated separately**. Pooling them would let one
carry the other, and they are different claims.

**If it fails, it is retired and documented.** Section 13.4.

## What would make this dishonest

Recorded so it can be checked against later:

- Adjusting any threshold above after seeing a result.
- Testing C-up and C-down, keeping the better, and reporting that.
- Dropping condition 7 to pool them if neither passes alone.
- Not counting this as variant 2.
