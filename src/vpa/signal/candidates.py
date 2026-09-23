"""Candidate filters - Concept v2 Section 7.

Two families, and both are **deliberately permissive**. Section 7 calls
them "candidate filters... designed for recall, not to make the final
judgement". They are not the signal; they decide which bars are worth
looking at. Making them stricter here would quietly do the AI layer's
job for it, and do it worse, because a filter cannot weigh evidence.

### Family A - effort with no result

A lot of trading happened and the price barely moved, near a level that
matters. Volume in the top decile for that hour of the day, a narrow
range, almost no net move even after the market's own move is removed,
and within 1.5 daily ATR of a recent high or low.

### Family B - pushed up and sold back

Heavy trading, price reached higher than it had in ten bars, could not
hold it, and closed near the bottom of its range - right where it
previously failed. Within 2.0 daily ATR of a resistance reference.

### What is **not** a filter here

`repeat_hv_narrow_5` and the other sequence counts are passed forward as
features. Section 7.1 says so explicitly: the filter stays permissive
and the repetition is evidence for the judgement layer, not a gate.

**Nor are the event flags.** Section 7 item 6 reads "No event flag on
that date *(validation only; in production, flagged and retained)*", so
excluding flagged days is not part of what makes a bar a candidate. It
is a mask applied afterwards, by validation only - see `unflagged`. Were
it baked in here, production would silently drop candidates on exactly
the days a trader most wants to see marked (LEDGER-5, reading 3).

### A candidate is a bar

Not a stock, and not a stock-day. Section 8.1 gives the AI "the
candidate bar's complete feature vector" and Section 8.2 returns a
`signal_timestamp`, so one stock can contribute several candidates in a
day, and the Section 7.3 budget counts bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --- Family A (Section 7.1) ---------------------------------------------------

#: Volume percentile for the same slot over the trailing 60 sessions.
MIN_VOL_PCT = 90

#: The bar's range, in daily ATR.
A_MAX_SPREAD_ATR = 0.60

#: Net move over the bar, and the same after the market's move is removed.
A_MAX_RET_ATR = 0.25
A_MAX_RESID_RET_ATR = 0.25

#: How near a level the bar has to be, in daily ATR.
A_LOCATION_ATR = 1.5

#: The levels Family A counts as "a level that matters".
A_LOCATIONS = ["dist_high_20", "dist_low_20", "dist_prior_week_high", "dist_prior_week_low"]

# --- Family B (Section 7.2) ---------------------------------------------------

#: How much of the bar's range is upper wick.
B_MIN_UPPER_WICK_FRAC = 0.50

#: Where the close sits in the bar's range: 0 is the low, 1 the high.
B_MAX_CLOSE_LOC = 0.35

B_LOCATION_ATR = 2.0

#: Resistance references only. A swing *low* is support, so the merged
#: nearest-pivot column cannot be used here (LEDGER-2, amendment 1).
#: Family B also requires the level to have been **tested**, not merely
#: to be nearby - see `tested_any`.
B_LOCATIONS = [
    "dist_high_20",
    "dist_prior_week_high",
    "dist_prior_month_high",
    "dist_nearest_pivot_high",
]

# --- Pattern C (pre-registered 2026-09-23, LEDGER-10) -------------------------
#
# Not in Concept v2. A third hypothesis, pre-registered in
# docs/pattern-c-preregistration.md **before** any test against history,
# and not yet validated. Families A and B each judge one bar; C judges a
# stretch, and unlike B it is symmetric.

#: At least this many of the previous five bars were busy and narrow.
C_MIN_REPEAT = 2

#: The stretch's total volume, against the same stretch on earlier days.
C_MIN_CUM_VOL_PCT = 90

#: Net movement across the stretch, in daily ATR. Effort, no result.
C_MAX_PROGRESS_ATR = 0.5

C_LOCATION_ATR = A_LOCATION_ATR

#: Where the bar must close for each direction.
C_UP_MIN_CLOSE_LOC = 0.60
C_DOWN_MAX_CLOSE_LOC = 0.40

#: The levels that make a setup bullish, and those that make it bearish.
C_LOW_LEVELS = ["dist_low_20", "dist_prior_week_low"]
C_HIGH_LEVELS = ["dist_high_20", "dist_prior_week_high"]

C_COLUMNS = ["repeat_hv_narrow_5", "cum_vol_pct_5", "progress_5", *C_LOW_LEVELS, *C_HIGH_LEVELS]


def family_c(features: pd.DataFrame, direction: str) -> pd.Series:
    """Pattern C, as pre-registered. `direction` is "up" or "down".

    Conditions 1 to 5 are the stretch: repeated busy-and-narrow bars,
    the stretch's volume in its own top decile, no net progress, and
    this bar busy too. Conditions 6 and 7 are where and which way.

    **Unvalidated.** Nothing about this has been tested against forward
    returns. It is wired into the daily scan so it can be watched, and
    every report says so.
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', not {direction!r}")
    missing = [column for column in C_COLUMNS if column not in features.columns]
    if missing:
        raise ValueError(f"features are missing columns Pattern C needs: {missing}")

    stretch = (
        _at_least(features["repeat_hv_narrow_5"], C_MIN_REPEAT)
        & _at_least(features["cum_vol_pct_5"], C_MIN_CUM_VOL_PCT)
        & _at_most(features["progress_5"].abs(), C_MAX_PROGRESS_ATR)
        & _at_least(features["vol_pct_slot_60"], MIN_VOL_PCT)
        & _is_good_quality(features)
    )
    to_low = _distances(features, C_LOW_LEVELS).abs().min(axis=1, skipna=True)
    to_high = _distances(features, C_HIGH_LEVELS).abs().min(axis=1, skipna=True)
    if direction == "up":
        placed = _at_most(to_low, C_LOCATION_ATR) & _at_most(to_low, to_high)
        closed = _at_least(features["close_loc"], C_UP_MIN_CLOSE_LOC)
    else:
        placed = _at_most(to_high, C_LOCATION_ATR) & _at_most(to_high, to_low)
        closed = _at_most(features["close_loc"], C_DOWN_MAX_CLOSE_LOC)
    return _decided(stretch & placed & closed)


def _at_most_series(values: pd.Series, limit: pd.Series) -> pd.Series:
    return pd.Series(
        values.to_numpy(dtype=float) <= limit.to_numpy(dtype=float), index=values.index
    )


# --- the budget (Section 7.3) -------------------------------------------------

#: Hard cap on candidates in a day, across the whole universe.
DAILY_CAP = 60

#: Section 7.3 also caps candidates at 8 per GICS sector. **Not
#: implemented**: no sector classification exists yet (Section 16, open
#: item 2; LEDGER-5, deviation 1). Left here named so it cannot be
#: mistaken for an oversight.
PER_SECTOR_CAP = 8

#: Family B's location test needs the bar's own prices, not just its
#: features: whether the level was reached is a fact about where price
#: went, and a distance from the close cannot answer it.
PRICE_COLUMNS = ["security_key", "date", "slot_index", "close", "low", "atr20"]

REQUIRED_COLUMNS = [
    "vol_pct_slot_60", "spread_atr", "ret_atr", "resid_ret_atr", "upper_wick_frac",
    "close_loc", "failed_new_high", "low_quality", *A_LOCATIONS, *B_LOCATIONS,
    *PRICE_COLUMNS,
]  # fmt: skip


def family_a(features: pd.DataFrame) -> pd.Series:
    """Section 7.1, every condition but the event flag.

    A bar whose features cannot be evaluated is not a candidate. That is
    what `_at_most` and `near_any` give us: a comparison against a
    missing value is false, never true, so a bar is never admitted on the
    strength of a number we do not have.
    """
    return _decided(
        _at_least(features["vol_pct_slot_60"], MIN_VOL_PCT)
        & _at_most(features["spread_atr"], A_MAX_SPREAD_ATR)
        & _at_most(features["ret_atr"].abs(), A_MAX_RET_ATR)
        & _at_most(features["resid_ret_atr"].abs(), A_MAX_RESID_RET_ATR)
        & _is_good_quality(features)
        & near_any(features, A_LOCATIONS, A_LOCATION_ATR)
    )


def family_b(features: pd.DataFrame) -> pd.Series:
    """Section 7.2, every condition but the event flag."""
    return _decided(
        _at_least(features["vol_pct_slot_60"], MIN_VOL_PCT)
        & _at_least(features["upper_wick_frac"], B_MIN_UPPER_WICK_FRAC)
        & _at_most(features["close_loc"], B_MAX_CLOSE_LOC)
        & features["failed_new_high"].eq(True)
        & _is_good_quality(features)
        & tested_any(features, B_LOCATIONS, B_LOCATION_ATR)
    )


def near_any(features: pd.DataFrame, references: list[str], limit: float) -> pd.Series:
    """Whether the bar is within `limit` daily ATR of any reference.

    Distance is absolute here, and that is right for **Family A**, whose
    condition 7 is a proximity test - "near a level that matters", highs
    and lows together. A stock a little above its 20-day high is near
    that level exactly as one a little below is.

    Family B is a different question and uses `tested_any` instead.
    """
    distances = _distances(features, references).abs()
    return _at_most(distances.min(axis=1, skipna=True), limit)


def tested_any(features: pd.DataFrame, references: list[str], limit: float) -> pd.Series:
    """Whether the bar is within `limit` ATR of a reference it **tested**.

    Family B is a resistance test: Section 7.2's plain-English gloss is
    "right where it previously failed". Mere proximity is not enough, and
    neither is the strict reading that price must close below the level.
    A level is tested when price reached it:

    - the bar **closes at or below** the level, the classic shape; or
    - price **crossed the level during the session** - at this bar or an
      earlier one - and closed back above it, a failed breakout.

    Both are rejections at the level. What this excludes is a stock that
    spent the whole session above the level and never came near it, which
    is not being resisted by it at all (LEDGER-5, reading 2).

    Measured over 242 sampled sessions: of the 276 Family B candidates
    whose close sits above every nearby level, **268 (97%) had price
    below that level earlier in the same session**. Requiring the close
    to be below would have discarded those; this rule keeps them and
    drops the 8 that were established above all day.
    """
    distances = _distances(features, references)
    lowest = session_low_so_far(features)
    atr = features["atr20"].to_numpy(dtype=float)
    close = features["close"].to_numpy(dtype=float)
    # How far the session's low so far sits below this bar's close, in
    # ATR. A level at distance `d` was reached when the drop covers it.
    drop = (close - lowest.to_numpy(dtype=float)) / np.where(atr > 0, atr, np.nan)

    tested = pd.Series(False, index=features.index)
    for column in references:
        distance = distances[column].to_numpy(dtype=float)
        within = np.abs(distance) <= limit
        reached = (distance <= 0) | (drop >= distance)
        tested |= pd.Series(within & reached, index=features.index)
    return tested


def session_low_so_far(features: pd.DataFrame) -> pd.Series:
    """The lowest price so far in each bar's session, including itself.

    Strictly backward-looking within the session: a bar never sees a low
    that happens after it.

    **The frame must hold every bar of the sessions it covers.** Pass a
    filtered subset - only the bars that already look like candidates,
    say - and the running low is taken over the wrong bars, quietly
    making Family B stricter than it should be. Filter *after* calling
    this, never before.
    """
    ordered = features.sort_values(["security_key", "date", "slot_index"], kind="stable")
    running = ordered.groupby(["security_key", "date"], sort=False)["low"].cummin()
    return running.reindex(features.index)


def _distances(features: pd.DataFrame, references: list[str]) -> pd.DataFrame:
    missing = [column for column in references if column not in features.columns]
    if missing:
        raise ValueError(f"features are missing location columns: {missing}")
    return features[references]


def _decided(mask: pd.Series) -> pd.Series:
    """A plain true or false for every bar, never "unknown".

    `failed_new_high` is a three-state flag, so comparing it carries NA
    through the whole chain: a bar that passes every other Family B test
    but has an unknown `failed_new_high` comes out NA rather than False.
    Section 7 admits a bar or it does not - there is no third answer -
    and an unevaluable bar is not a candidate (LEDGER-5, reading 5).

    This is enforced here rather than left to each caller, because an NA
    leaking into `is_candidate` fails far downstream and confusingly:
    it surfaced as a crash 400 sessions into the outcomes build.
    """
    return mask.astype("boolean").fillna(False).astype(bool)


def _at_least(values: pd.Series, threshold: float) -> pd.Series:
    return pd.Series(values.to_numpy(dtype=float) >= threshold, index=values.index)


def _at_most(values: pd.Series, threshold: float) -> pd.Series:
    return pd.Series(values.to_numpy(dtype=float) <= threshold, index=values.index)


def _is_good_quality(features: pd.DataFrame) -> pd.Series:
    """Section 7's `low_quality is false`. A bar with fewer than five
    minutes of trades can never be a candidate (Section 4.3)."""
    return ~features["low_quality"].eq(True)


def candidates(features: pd.DataFrame) -> pd.DataFrame:
    """Which bars are candidates, and under which family.

    A bar can satisfy both families; it is still one bar, one candidate,
    and counts once against the Section 7.3 budget.
    """
    missing = [column for column in REQUIRED_COLUMNS if column not in features.columns]
    if missing:
        raise ValueError(f"features are missing columns Section 7 needs: {missing}")
    in_a, in_b = family_a(features), family_b(features)
    marked = features.assign(family_a=in_a, family_b=in_b, is_candidate=in_a | in_b)
    if all(column in features.columns for column in C_COLUMNS):
        # Pattern C is recorded but kept **out** of `is_candidate`: it is
        # unvalidated, and Sections 11 and 13 are measuring A and B. A
        # third family silently joining the candidate set would change
        # what those numbers mean (LEDGER-10).
        marked = marked.assign(
            family_c_up=family_c(features, "up"), family_c_down=family_c(features, "down")
        )
    return marked


# --- the daily budget (Section 7.3) -------------------------------------------


def apply_cap(day: pd.DataFrame, cap: int = DAILY_CAP) -> tuple[pd.DataFrame, int]:
    """One session's candidates, trimmed to the budget, and how many were
    dropped.

    Section 7.3 ranks by `vol_pct_slot_60` descending. That alone is not
    deterministic - the percentile is a coarse number and ties are
    commonplace, with whole groups of bars sitting at 100 - so ticker and
    then slot break them. Without that, which candidates survived a busy
    day would depend on the order rows happened to be read from disk.

    **The count dropped is returned, never discarded.** Section 7.3: "a
    systematically truncated candidate set changes the meaning of every
    downstream statistic".
    """
    firing = day[day["is_candidate"]]
    if len(firing) <= cap:
        return firing, 0
    ranked = firing.sort_values(
        ["vol_pct_slot_60", "requested_ticker", "slot_index"],
        ascending=[False, True, True],
        kind="stable",
    )
    return ranked.head(cap), len(firing) - cap


# --- the validation mask (Section 7 item 6) -----------------------------------


def unflagged(events: pd.DataFrame, strict: bool = True) -> pd.Series:
    """Which rows validation may use, by Section 7's event condition.

    Applied **only** in validation. Section 6 and Section 7 both say
    production keeps flagged days and marks them.

    `strict` decides what to do where a flag is *unknown* rather than
    false - a company we hold no results filings for, or whose filing
    record went quiet (LEDGER-4):

    - `True` (the project owner's decision, LEDGER-5): a day is usable
      only when every observable flag is known false. An unverifiable day
      is not a clean day.
    - `False`: only days with a flag actually true are excluded.

    Both are computed and reported, so the difference between them is
    measured rather than assumed - also the owner's decision.
    """
    if "any_event" not in events.columns:
        raise ValueError("event rows need an any_event column")
    flag = events["any_event"]
    # A three-state flag propagates unknown through a comparison, so each
    # reading has to say plainly what it does with it rather than leave
    # an unknown to drift downstream as a third answer.
    usable = flag.eq(False).fillna(False) if strict else flag.ne(True).fillna(True)
    return usable.astype(bool)


def cap_report(kept: int, dropped: int, session) -> dict:
    """What Section 7.3 requires to be recorded about a truncated day."""
    return {
        "date": session,
        "candidates": kept + dropped,
        "kept": kept,
        "dropped": dropped,
        "capped": bool(dropped),
    }


def summarise(day: pd.DataFrame) -> dict:
    """Counts for one session's candidates, before any cap."""
    return {
        "bars": len(day),
        "family_a": int(day["family_a"].sum()),
        "family_b": int(day["family_b"].sum()),
        "both": int((day["family_a"] & day["family_b"]).sum()),
        "candidates": int(day["is_candidate"].sum()),
        "securities": int(day.loc[day["is_candidate"], "security_key"].nunique())
        if "security_key" in day.columns
        else np.nan,
    }
