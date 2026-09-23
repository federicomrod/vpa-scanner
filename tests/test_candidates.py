"""Tests for the Section 7 candidate filters.

Built on hand-made feature rows, so every threshold is checked at its
exact boundary rather than "somewhere near". No network, no real data.

The real-data proof - ANF's gap day of 13 January 2025 - lives in
`scripts/check_candidates.py`, because it needs the store.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.signal.candidates import (
    A_LOCATION_ATR,
    A_LOCATIONS,
    A_MAX_RESID_RET_ATR,
    A_MAX_RET_ATR,
    A_MAX_SPREAD_ATR,
    B_LOCATION_ATR,
    B_LOCATIONS,
    B_MAX_CLOSE_LOC,
    B_MIN_UPPER_WICK_FRAC,
    DAILY_CAP,
    MIN_VOL_PCT,
    PER_SECTOR_CAP,
    apply_cap,
    candidates,
    family_a,
    family_b,
    near_any,
    session_low_so_far,
    summarise,
    unflagged,
)

# A row that satisfies Family A everywhere, so each test can break one
# thing and see only that break.
PASSES_A = {
    "vol_pct_slot_60": 95.0,
    "spread_atr": 0.30,
    "ret_atr": 0.10,
    "resid_ret_atr": 0.10,
    "upper_wick_frac": 0.10,
    "close_loc": 0.50,
    "failed_new_high": False,
    "low_quality": False,
    "dist_high_20": 5.0,
    "dist_low_20": 0.20,
    "dist_prior_week_high": 5.0,
    "dist_prior_week_low": 4.0,
    "dist_prior_month_high": 6.0,
    "dist_nearest_pivot_high": 7.0,
    "requested_ticker": "FAKEA",
    "security_key": "FIGI_FAKEA",
    "date": date(2025, 3, 12),
    "slot_index": 3,
    "close": 100.0,
    "low": 99.0,
    "atr20": 2.0,
}

# The same for Family B: pushed up, sold back, into resistance.
# Family B: pushed up, sold back, and the level was tested. The close
# sits 0.50 ATR above the 20-day high (at 99.0) and the bar's low
# reached it, so this is a failed breakout rather than a stock that
# spent the day clear of the level.
PASSES_B = {
    **PASSES_A,
    "upper_wick_frac": 0.70,
    "close_loc": 0.20,
    "failed_new_high": True,
    "dist_high_20": 0.50,
    "low": 98.5,
}


def rows(*overrides: dict, base: dict | None = None) -> pd.DataFrame:
    """Feature rows built from a passing template."""
    template = base if base is not None else PASSES_A
    return pd.DataFrame([{**template, **o} for o in (overrides or ({},))])


def a(*overrides: dict) -> pd.Series:
    return family_a(rows(*overrides))


def b(*overrides: dict) -> pd.Series:
    return family_b(rows(*overrides, base=PASSES_B))


# --- Family A, condition by condition ----------------------------------------


def test_the_template_is_a_family_a_candidate():
    assert a().iloc[0]


def test_volume_must_reach_the_ninetieth_percentile():
    assert MIN_VOL_PCT == 90
    assert a({"vol_pct_slot_60": 90.0}).iloc[0]
    assert not a({"vol_pct_slot_60": 89.99}).iloc[0]


def test_the_bar_must_be_narrow():
    assert A_MAX_SPREAD_ATR == 0.60
    assert a({"spread_atr": 0.60}).iloc[0]
    assert not a({"spread_atr": 0.601}).iloc[0]


def test_the_price_must_barely_have_moved_in_either_direction():
    assert A_MAX_RET_ATR == 0.25
    assert a({"ret_atr": 0.25}).iloc[0]
    assert a({"ret_atr": -0.25}).iloc[0]
    assert not a({"ret_atr": 0.251}).iloc[0]
    assert not a({"ret_atr": -0.251}).iloc[0]


def test_it_must_not_have_moved_once_the_market_is_removed_either():
    assert A_MAX_RESID_RET_ATR == 0.25
    # The whole point of condition 4: the stock was flat on the day, but
    # only because the market carried it. That is not an anomaly.
    assert not a({"ret_atr": 0.05, "resid_ret_atr": 0.40}).iloc[0]


def test_a_low_quality_bar_can_never_be_a_candidate():
    assert not a({"low_quality": True}).iloc[0]


def test_it_must_be_near_one_of_the_four_levels():
    assert A_LOCATION_ATR == 1.5
    far = dict.fromkeys(A_LOCATIONS, 9.0)
    assert not a(far).iloc[0]
    assert a({**far, "dist_prior_week_low": 1.5}).iloc[0]
    assert not a({**far, "dist_prior_week_low": 1.51}).iloc[0]
    # Either side of the level counts: 1.4 above is as near as 1.4 below.
    assert a({**far, "dist_high_20": -1.4}).iloc[0]
    assert a({**far, "dist_high_20": 1.4}).iloc[0]


def test_family_a_looks_at_highs_and_lows_both():
    assert A_LOCATIONS == [
        "dist_high_20",
        "dist_low_20",
        "dist_prior_week_high",
        "dist_prior_week_low",
    ]


# --- Family B, condition by condition ----------------------------------------


def test_the_template_is_a_family_b_candidate():
    assert b().iloc[0]


def test_most_of_the_bar_must_be_upper_wick():
    assert B_MIN_UPPER_WICK_FRAC == 0.50
    assert b({"upper_wick_frac": 0.50}).iloc[0]
    assert not b({"upper_wick_frac": 0.499}).iloc[0]


def test_it_must_close_near_its_low():
    assert B_MAX_CLOSE_LOC == 0.35
    assert b({"close_loc": 0.35}).iloc[0]
    assert not b({"close_loc": 0.351}).iloc[0]


def test_it_must_have_reached_higher_and_failed_to_hold_it():
    assert not b({"failed_new_high": False}).iloc[0]
    assert not b({"failed_new_high": pd.NA}).iloc[0]


def three_state(value) -> pd.DataFrame:
    """A row whose `failed_new_high` has the dtype the stored features
    actually use - nullable boolean, not object."""
    frame = rows({"failed_new_high": value}, base=PASSES_B)
    return frame.assign(failed_new_high=frame["failed_new_high"].astype("boolean"))


def test_an_unknown_failed_new_high_decides_false_not_unknown():
    # The stored features hold this as a nullable boolean, where the
    # comparison carries NA through the whole chain. Built from a dict
    # the column is object dtype, where it quietly becomes False - so
    # the test above passed for the wrong reason and a crash surfaced
    # 400 sessions into the outcomes build instead (LEDGER-5).
    result = family_b(three_state(pd.NA))
    assert result.dtype == bool
    assert result.iloc[0] == False  # noqa: E712


def test_is_candidate_is_always_a_plain_yes_or_no():
    marked = candidates(three_state(pd.NA))
    assert marked["is_candidate"].dtype == bool
    assert marked["family_a"].dtype == bool and marked["family_b"].dtype == bool


def test_a_known_failed_new_high_still_decides_normally():
    assert family_b(three_state(True)).iloc[0]
    assert not family_b(three_state(False)).iloc[0]


def test_family_b_measures_resistance_only():
    # A swing low is support. If Family B could satisfy its location test
    # on one, it would fire on stocks sitting at the bottom of a range -
    # the opposite of the pattern (LEDGER-2, amendment 1).
    assert B_LOCATIONS == [
        "dist_high_20",
        "dist_prior_week_high",
        "dist_prior_month_high",
        "dist_nearest_pivot_high",
    ]
    assert "dist_nearest_swing_pivot" not in B_LOCATIONS
    assert not any("low" in column for column in B_LOCATIONS)


def test_family_b_allows_a_wider_berth_than_family_a():
    assert B_LOCATION_ATR == 2.0
    far = dict.fromkeys(B_LOCATIONS, 9.0)
    # Below the level, so it is tested by closing under it.
    assert b({**far, "dist_nearest_pivot_high": -2.0}).iloc[0]
    assert not b({**far, "dist_nearest_pivot_high": -2.01}).iloc[0]


# --- Family B tests the level, it does not merely sit near it ----------------


def test_closing_below_the_level_tests_it():
    far = dict.fromkeys(B_LOCATIONS, 9.0)
    assert b({**far, "dist_high_20": -0.5, "low": 99.9}).iloc[0]


def test_closing_above_a_level_the_bar_reached_is_a_failed_breakout():
    # Close 0.5 ATR (1.0) above the level at 99.0, and the session's low
    # of 98.5 went through it. Price tested the level and was rejected;
    # it simply settled back a fraction above it.
    far = dict.fromkeys(B_LOCATIONS, 9.0)
    assert b({**far, "dist_high_20": 0.5, "low": 98.5}).iloc[0]


def test_a_stock_that_never_came_near_the_level_is_not_tested():
    # Same close, same level, but the session's low never reached it.
    # Nothing here was resisted by anything.
    far = dict.fromkeys(B_LOCATIONS, 9.0)
    assert not b({**far, "dist_high_20": 0.5, "low": 99.5}).iloc[0]


def test_a_level_reached_earlier_in_the_session_still_counts():
    # The candidate is slot 3 and never dips to the level itself, but
    # slot 1 did. The level was crossed during the session.
    session = pd.DataFrame(
        [
            {**PASSES_B, "slot_index": 1, "low": 98.0, "vol_pct_slot_60": 10.0},
            {**PASSES_B, "slot_index": 3, "low": 99.6},
        ]
    )
    assert bool(family_b(session).iloc[1])


def test_a_low_later_in_the_session_cannot_rescue_an_earlier_bar():
    # Look-ahead: slot 5 reaching the level says nothing about slot 3.
    session = pd.DataFrame(
        [
            {**PASSES_B, "slot_index": 3, "low": 99.6},
            {**PASSES_B, "slot_index": 5, "low": 98.0, "vol_pct_slot_60": 10.0},
        ]
    )
    assert not bool(family_b(session).iloc[0])


def test_one_securitys_session_low_does_not_reach_into_anothers():
    session = pd.DataFrame(
        [
            {**PASSES_B, "security_key": "FIGI_A", "slot_index": 1, "low": 98.0,
             "vol_pct_slot_60": 10.0},
            {**PASSES_B, "security_key": "FIGI_B", "slot_index": 3, "low": 99.6},
        ]
    )  # fmt: skip
    assert not bool(family_b(session).iloc[1])


def test_the_running_low_is_taken_over_the_whole_session():
    # The trap this documents: filtering to the bars that already look
    # like candidates, and only then asking whether the level was
    # reached, takes the running low over the wrong bars. Slot 1 is the
    # bar that crossed the level, and it is not a candidate itself.
    session = pd.DataFrame(
        [
            {**PASSES_B, "slot_index": 1, "low": 98.0, "vol_pct_slot_60": 10.0},
            {**PASSES_B, "slot_index": 3, "low": 99.6},
        ]
    )
    assert bool(family_b(session).iloc[1])
    # Drop slot 1 first and the same bar stops qualifying - quietly.
    only_shaped = session[session["vol_pct_slot_60"] >= MIN_VOL_PCT]
    assert not bool(family_b(only_shaped).iloc[0])


def test_yesterdays_low_does_not_test_todays_level():
    # A level is tested by what price did *this* session. Carrying
    # yesterday's low forward would let a bar inherit a test it never
    # made - and the levels themselves move daily.
    session = pd.DataFrame(
        [
            {**PASSES_B, "date": date(2025, 3, 11), "slot_index": 3, "low": 98.0,
             "vol_pct_slot_60": 10.0},
            {**PASSES_B, "date": date(2025, 3, 12), "slot_index": 3, "low": 99.6},
        ]
    )  # fmt: skip
    assert not bool(family_b(session).iloc[1])
    assert list(session_low_so_far(session)) == [98.0, 99.6]


def test_the_running_low_never_looks_past_the_bar():
    session = pd.DataFrame(
        [{**PASSES_B, "slot_index": n, "low": 99.8 if n < 5 else 90.0} for n in range(7)]
    )
    lows = session_low_so_far(session)
    assert list(lows)[:5] == [99.8] * 5
    assert lows.iloc[5] == 90.0


def test_family_a_still_only_asks_how_near_the_level_is():
    # Family A's condition 7 is proximity, not a resistance test: it
    # names lows as well as highs, so "did price reach it" is not the
    # question being asked.
    far = dict.fromkeys(A_LOCATIONS, 9.0)
    assert a({**far, "dist_high_20": 1.0, "low": 99.9}).iloc[0]


# --- missing numbers ---------------------------------------------------------


def test_a_bar_is_never_admitted_on_a_number_we_do_not_have():
    for column in ("vol_pct_slot_60", "spread_atr", "ret_atr", "resid_ret_atr"):
        assert not a({column: float("nan")}).iloc[0], column


def test_a_missing_distance_does_not_count_as_near():
    far = dict.fromkeys(A_LOCATIONS, float("nan"))
    assert not a(far).iloc[0]
    # ...but one known-near level is enough, even if the others are missing.
    assert a({**far, "dist_low_20": 0.5}).iloc[0]


def test_a_missing_location_column_is_an_error_not_a_silent_false():
    with pytest.raises(ValueError, match="missing location columns"):
        near_any(rows().drop(columns=["dist_low_20"]), A_LOCATIONS, A_LOCATION_ATR)


def test_features_missing_a_column_section_7_needs_are_an_error():
    with pytest.raises(ValueError, match="missing columns Section 7 needs"):
        candidates(rows().drop(columns=["close_loc"]))


# --- putting both families together ------------------------------------------


def test_a_bar_can_satisfy_both_families_and_is_still_one_candidate():
    both = rows(base=PASSES_B)
    result = candidates(both.assign(spread_atr=0.30, ret_atr=0.10, resid_ret_atr=0.10))
    assert result["family_a"].iloc[0] and result["family_b"].iloc[0]
    assert result["is_candidate"].sum() == 1
    assert summarise(result)["both"] == 1


def test_a_bar_satisfying_neither_is_not_a_candidate():
    result = candidates(rows({"vol_pct_slot_60": 10.0}))
    assert not result["is_candidate"].iloc[0]


def test_the_sequence_counts_are_not_filters():
    # Section 7.1: repetition is passed forward as evidence, not used as
    # a gate. A bar with no repetition behind it is still a candidate.
    result = candidates(rows().assign(repeat_hv_narrow_5=0))
    assert result["is_candidate"].iloc[0]


# --- the daily budget --------------------------------------------------------


def many(n: int, quiet: int = 0) -> pd.DataFrame:
    """`n` bars that would each be a Family A candidate, the first
    `quiet` of them made too dull to qualify."""
    table = pd.DataFrame(
        [
            {**PASSES_A, "requested_ticker": f"T{i:03d}", "security_key": f"FIGI_{i:03d}"}
            for i in range(n)
        ]
    )
    table.loc[table.index[:quiet], "vol_pct_slot_60"] = 10.0
    return candidates(table)


def test_a_quiet_day_is_left_alone():
    kept, dropped = apply_cap(many(10))
    assert len(kept) == 10 and dropped == 0


def test_a_busy_day_is_cut_to_the_cap_and_the_loss_recorded():
    assert DAILY_CAP == 60
    kept, dropped = apply_cap(many(75))
    assert len(kept) == 60
    assert dropped == 15


def test_the_busiest_bars_are_the_ones_kept():
    table = many(70)
    table.loc[table.index[:5], "vol_pct_slot_60"] = 91.0  # the least busy
    kept, _ = apply_cap(table)
    assert not set(table["requested_ticker"][:5]) & set(kept["requested_ticker"])


def test_the_cap_is_reproducible_when_volume_ties():
    # Percentiles are coarse and whole groups sit at 100. Without a
    # tiebreak, which candidates survived would depend on the order rows
    # came off disk.
    table = many(70)
    first, _ = apply_cap(table)
    shuffled, _ = apply_cap(table.sample(frac=1, random_state=7))
    assert list(first["requested_ticker"]) == list(shuffled["requested_ticker"])


def test_only_candidates_are_counted_against_the_budget():
    kept, dropped = apply_cap(many(70, quiet=40))
    assert len(kept) == 30 and dropped == 0


def test_the_sector_cap_is_recorded_as_unimplemented():
    # Section 7.3 also caps at 8 per GICS sector. There is no sector
    # classification yet (Section 16, open item 2), so the constant
    # exists but nothing applies it - deliberately, and on the record.
    assert PER_SECTOR_CAP == 8


# --- the event mask (Section 7 item 6) ---------------------------------------


def events(*values) -> pd.DataFrame:
    return pd.DataFrame({"any_event": pd.array(list(values), dtype="boolean")})


def test_strictly_only_a_known_clean_day_is_used():
    assert list(unflagged(events(False, True, None), strict=True)) == [True, False, False]


def test_leniently_an_unknown_day_is_kept():
    assert list(unflagged(events(False, True, None), strict=False)) == [True, False, True]


def test_the_event_mask_is_not_part_of_being_a_candidate():
    # Section 7 item 6 is "validation only; in production, flagged and
    # retained". A bar on an earnings day is still a candidate - it is
    # marked, not dropped, or the trader would never see the warning.
    result = candidates(rows())
    assert result["is_candidate"].iloc[0]
    assert "any_event" not in result.columns


def test_event_rows_without_the_combined_flag_are_an_error():
    with pytest.raises(ValueError, match="any_event"):
        unflagged(pd.DataFrame({"earnings": [False]}))


# --- summaries ---------------------------------------------------------------


def test_a_summary_counts_bars_families_and_distinct_stocks():
    found = summarise(many(5, quiet=2))
    assert found == {
        "bars": 5,
        "family_a": 3,
        "family_b": 0,
        "both": 0,
        "candidates": 3,
        "securities": 3,
    }


def test_the_thresholds_are_the_ones_the_frozen_spec_names():
    # Read straight off Section 7. If any of these ever needs changing,
    # it is a Class 2 change with a ledger entry, not an edit.
    assert (MIN_VOL_PCT, A_MAX_SPREAD_ATR, A_MAX_RET_ATR, A_MAX_RESID_RET_ATR) == (
        90,
        0.60,
        0.25,
        0.25,
    )
    assert (A_LOCATION_ATR, B_MIN_UPPER_WICK_FRAC, B_MAX_CLOSE_LOC, B_LOCATION_ATR) == (
        1.5,
        0.50,
        0.35,
        2.0,
    )
    assert (DAILY_CAP, PER_SECTOR_CAP) == (60, 8)


def test_dates_are_not_needed_to_decide_a_candidate():
    # Section 7 is a test on one bar's features. Nothing here looks at
    # neighbouring bars, so a candidate cannot depend on the future.
    assert candidates(rows().assign(date=date(2025, 1, 13)))["is_candidate"].iloc[0]
