"""Tests for the matched controls (src/vpa/signal/controls.py).

Hand-built universes, so every decile boundary and every fallback is
exact. No network, no real data.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.signal.controls import (
    CONTROLS_PER_CANDIDATE,
    DECILES,
    MAX_WIDENING,
    attach_deciles,
    cell_sizes,
    deciles,
    match,
    mean_cell_occupancy,
    relative_atr,
    summarise,
)

SESSION = date(2025, 3, 12)


def universe(n: int, candidates: list[int] | None = None, **overrides) -> pd.DataFrame:
    """`n` members, evenly spread across both decile axes."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "date": SESSION,
                "security_key": f"FIGI_{i:04d}",
                "atr_decile": i % DECILES,
                "volume_decile": (i // DECILES) % DECILES,
                "is_candidate": i in (candidates or []),
                "slot_index": 3,
            }
        )
    return pd.DataFrame(rows).assign(**overrides)


# --- deciles -----------------------------------------------------------------


def test_deciles_split_the_universe_into_ten_equal_parts():
    values = pd.Series(range(400))
    found = deciles(values)
    assert found.min() == 0 and found.max() == DECILES - 1
    assert found.value_counts().nunique() == 1  # 40 in each


def test_ties_are_broken_so_the_cells_stay_even():
    # Every stock at the same value: without first-rank tie-breaking
    # they would all land in one decile.
    found = deciles(pd.Series([7.0] * 100))
    assert found.nunique() == DECILES


def test_a_universe_too_small_to_split_has_no_deciles():
    assert deciles(pd.Series([1.0, 2.0, 3.0])).isna().all()


def test_a_missing_value_gets_no_decile():
    found = deciles(pd.Series([*range(20), None]))
    assert pd.isna(found.iloc[-1])
    assert found.iloc[:20].notna().all()


def test_relative_atr_measures_volatility_not_price():
    # Two stocks equally volatile in percentage terms, ten times apart
    # in price. Matching on raw ATR would separate them; this does not.
    found = relative_atr(pd.Series([10.0, 1.0]), pd.Series([500.0, 50.0]))
    assert found.iloc[0] == pytest.approx(found.iloc[1])


def test_a_zero_or_negative_price_has_no_relative_atr():
    found = relative_atr(pd.Series([1.0, 1.0]), pd.Series([0.0, -5.0]))
    assert found.isna().all()


def test_deciles_are_attached_from_the_columns_section_11_names():
    day = pd.DataFrame(
        {
            "security_key": [f"F{i}" for i in range(100)],
            "daily_atr": [1.0 + i * 0.01 for i in range(100)],
            "close": [100.0] * 100,
            "median_dollar_volume_60": [1e6 * (i + 1) for i in range(100)],
        }
    )
    found = attach_deciles(day)
    assert set(found["atr_decile"]) == set(range(DECILES))
    assert set(found["volume_decile"]) == set(range(DECILES))


def test_missing_columns_are_an_error():
    with pytest.raises(ValueError, match="control matching needs columns"):
        attach_deciles(pd.DataFrame({"close": [1.0]}))


# --- matching ----------------------------------------------------------------


def test_each_candidate_draws_five_controls():
    assert CONTROLS_PER_CANDIDATE == 5
    matched = match(universe(400, candidates=[0, 55]))
    assert len(matched) == 2 * CONTROLS_PER_CANDIDATE
    assert set(matched["candidate_key"]) == {"FIGI_0000", "FIGI_0055"}


def test_a_control_is_never_a_candidate_itself():
    # The comparison is against stocks that did not trip the filter.
    matched = match(universe(400, candidates=list(range(50))))
    assert not set(matched["control_key"]) & set(matched["candidate_key"])


def test_controls_come_from_the_same_decile_cell_when_it_is_full_enough():
    # 1,000 members puts ten in every cell. A real 400-stock universe
    # holds four, which is why widening is the norm - see below.
    day = universe(1000, candidates=[0])
    matched = match(day)
    assert (matched["widening"] == 0).all()
    picked = day[day["security_key"].isin(matched["control_key"])]
    assert (picked["atr_decile"] == 0).all()
    assert (picked["volume_decile"] == 0).all()


def test_a_thin_cell_relaxes_by_one_decile_and_says_so():
    # 100 members means one per cell - far short of five, so the match
    # has to reach into the neighbouring deciles.
    matched = match(universe(100, candidates=[55]))
    assert len(matched) == CONTROLS_PER_CANDIDATE
    assert (matched["widening"] == 1).all()


def test_a_real_sized_universe_needs_widening_too():
    # The finding that matters: 400 stocks over 100 cells is four per
    # cell, so an exact decile match usually cannot supply five.
    matched = match(universe(400, candidates=[0]))
    assert len(matched) == CONTROLS_PER_CANDIDATE
    assert (matched["widening"] == 1).all()


def test_widening_stops_rather_than_matching_anything_at_all():
    # A universe too small to find five even after the widest allowed
    # relaxation returns nothing, rather than a meaningless match.
    tiny = universe(12, candidates=[0])
    tiny.loc[1:, ["atr_decile", "volume_decile"]] = 9  # everything far away
    assert match(tiny).empty


def test_a_candidate_with_no_decile_is_not_matched():
    day = universe(400, candidates=[0])
    day.loc[0, "atr_decile"] = None
    assert match(day).empty


# --- reproducibility ---------------------------------------------------------


def test_the_same_candidate_always_draws_the_same_controls():
    day = universe(400, candidates=[0])
    first = list(match(day)["control_key"])
    shuffled = day.sample(frac=1, random_state=11)
    assert list(match(shuffled)["control_key"]) == first


def test_two_candidates_in_one_cell_do_not_draw_identical_controls():
    # Both are in cell (0, 0). If the draw ignored which candidate it was
    # for, every candidate in a cell would share one control set and the
    # comparison would be far less independent than it looks.
    day = universe(400, candidates=[0, 100])
    matched = match(day)
    first = set(matched[matched["candidate_key"] == "FIGI_0000"]["control_key"])
    second = set(matched[matched["candidate_key"] == "FIGI_0100"]["control_key"])
    assert first != second


def test_a_candidates_own_slot_changes_its_draw():
    # Two bars of the same stock on the same day are two candidates, and
    # each draws its own controls.
    day = universe(400, candidates=[0])
    other = day.copy()
    other.loc[0, "slot_index"] = 5
    assert list(match(day)["control_key"]) != list(match(other)["control_key"])


# --- reporting ---------------------------------------------------------------


def test_the_summary_says_how_much_widening_was_needed():
    matched = match(universe(100, candidates=[55]))
    found = summarise(matched, candidates=1, per_candidate=CONTROLS_PER_CANDIDATE)
    assert found["matched"] == 1
    assert found["exact"] == 0
    assert found["widened"] == CONTROLS_PER_CANDIDATE
    assert found["mean_widening"] == 1.0


def test_an_unmatched_candidate_is_counted_not_hidden():
    found = summarise(pd.DataFrame(columns=["candidate_key"]), candidates=3, per_candidate=5)
    assert found["unmatched"] == 3
    assert found["matched"] == 0


def test_cell_occupancy_is_what_the_arithmetic_says():
    # 400 stocks over 100 cells. Section 11.1 asks for five per cell,
    # which does not fit - the reason widening is the norm, not an
    # accident (LEDGER-7, reading 2).
    assert mean_cell_occupancy(400) == 4.0
    assert mean_cell_occupancy(400) < CONTROLS_PER_CANDIDATE


def test_cell_sizes_count_only_eligible_controls():
    day = universe(400, candidates=[0, 1, 2])
    sizes = cell_sizes(day)
    assert sizes.sum() == 397


def test_the_widening_limit_is_recorded():
    assert MAX_WIDENING == 3
