# LEDGER-9: Section 13.1 measured on the development window

**Date:** 2026-09-23
**Class:** measurement. No thresholds, rules or code in the frozen area
changed as a result, and none should change as a result.
**Counts as one variant** for Section 11.4's false-discovery-rate
correction. This is the first time the criterion has been run.

**The holdout was not touched.** Everything below stops at 2025-09-18.

## The result

| | Family A | Family B |
|---|---|---|
| candidate bars | 52,729 | 8,761 |
| dates (clusters) | 2,112 | 1,962 |
| mean abs. 5-day move, candidates | 1.5066 ATR | 1.7045 ATR |
| mean abs. 5-day move, controls | 1.4508 ATR | 1.4712 ATR |
| **edge** | **+0.0558** | **+0.2333** |
| standard error (date-clustered) | 0.0276 | 0.0743 |
| p | 0.043 | **0.0017** |
| Section 13.1 needs | >= 0.15 ATR, p < 0.01, >= 2 of 3 folds | |

### Family A does not earn its place

The edge is **a third of what Section 13.1 requires**, and it is stable
in its failure: +0.052, +0.056, +0.035 across the three folds, none
close to 0.15, none with p below 0.2.

**Calibration.** A placebo - each day's controls split at random in half
and compared against each other - returns **+0.0298 ATR (p = 0.09)**.
Family A's +0.0558 is under twice that. Whatever it is measuring is
close to the noise floor of the measurement itself.

### Family B clears the aggregate bar, with two caveats

Edge +0.2333 ATR at p = 0.0017: both thresholds met, and about eight
times the placebo. But:

**The fold requirement is ambiguous** (see below), and Family B passes
one reading and fails the other.

**The effect declines across the window**, monotonically:

| fold | edge | p |
|---|---|---|
| 2017-03 to 2019-11 | +0.3841 | 0.053 |
| 2020-02 to 2022-10 | +0.2429 | 0.040 |
| 2023-01 to 2025-09 | +0.1116 | 0.0076 |

The most recent third is **below** the 0.15 threshold. The p-value
improves only because candidates grew more numerous, not because the
effect strengthened. A pattern that worked in 2017 and is fading is a
different proposition from one that works.

## The ambiguity that decides Family B

Section 13.1: "... exceeds matched controls by >= 0.15 ATR, at p < 0.01
with date-clustered standard errors, **holding in >= 2 of 3
walk-forward folds**."

It does not say what must hold per fold:

- **The effect** (edge >= 0.15): Family B holds in **2 of 3** - it
  passes.
- **The whole criterion** (edge >= 0.15 *and* p < 0.01): Family B holds
  in **0 of 3** - it fails.

The strict reading is close to unachievable by construction. A fold has
a third of the data, so its standard error is about 1.7 times the
aggregate's; demanding the aggregate's p-value inside each third asks
for an effect far larger than the criterion itself names. **Raised with
the project owner rather than settled here.**

## Two errors made and corrected while producing this

### Twelve folds instead of three

The first run cut the window into twelve six-month folds. Family B then
passed the aggregate and **zero of twelve folds**, which looked like
damning instability and was an artefact: a twelfth of the data carries
about three and a half times the standard error, so no fold could reach
p < 0.01 whatever the effect.

Section 13.1 says "2 of 3", and Section 11.4's 18-month train, 6-month
test and 3-month purge come to 27 months a fold - about three across
the development window. Both readings agree on the count; my
construction did not match either.

### A three-state flag leaking into a two-state answer

`family_b` could return **unknown** rather than true or false, because
`failed_new_high` is a three-state flag and the comparison carried the
unknown through the chain. `is_candidate` could then be unknown too,
which crashed the outcomes build 400 sessions in.

The unit test written for exactly this case had passed - because a
DataFrame built from a dict makes the column `object` dtype, where the
comparison quietly yields false, while the stored features use nullable
`boolean`, where it yields unknown. **The second test in this project to
pass for the wrong reason** (LEDGER-2 records the first). The regression
test now builds the column with the dtype the store actually uses.

## What must not happen next

Section 13.4: "If the criteria are not met, the pattern family is
retired and documented. **This is a successful outcome of the project,
not a failure.**"

Section 10 "must never be optimised". Section 7's thresholds are frozen.
The correct responses to this result are to accept it, to fix something
demonstrably wrong with the measurement, or to retire a family - not to
adjust a threshold until the number improves. Every variant tried is
counted against the false-discovery correction, so searching for a
passing configuration makes the eventual answer weaker, not stronger.

## Reproducing this

`uv run python scripts/check_kill_criterion.py --family a|b|both`, which
prints match quality above the result at the project owner's request:
of 304,180 controls drawn, **29.5% matched in the same decile cell,
70.4% within one decile**, 0.03% further out, average widening 0.71.
