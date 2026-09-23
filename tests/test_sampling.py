"""Tests for the blind label sampling (src/vpa/signal/sampling.py).

Hand-built populations, so every share and weight is exact. No network,
no real data, and nothing here ever touches a real label.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from vpa.signal.sampling import (
    CANDIDATE_SHARE,
    CANDIDATE_STRATUM,
    CHARTS_PER_SESSION,
    POPULATION_STRATUM,
    RETEST_GAP_DAYS,
    RETEST_SHARE,
    TARGET_LABELS,
    draw,
    due_for_retest,
    inverse_probability_weight,
    sessions_to_target,
    split,
)

SESSION = date(2025, 3, 12)


def bars(total: int = 2750, candidates: int = 30) -> pd.DataFrame:
    """A day's universe bars, the first `candidates` of them firing."""
    return pd.DataFrame(
        [
            {
                "security_key": f"FIGI_{i:05d}",
                "date": SESSION,
                "slot_index": i % 7,
                "is_candidate": i < candidates,
            }
            for i in range(total)
        ]
    )


# --- the strata --------------------------------------------------------------


def test_the_strata_are_disjoint():
    # Overlapping strata would make the weights wrong in a way that is
    # easy to get silently wrong (LEDGER-8, reading 2).
    frame = split(bars())
    assert frame.sizes == {CANDIDATE_STRATUM: 30, POPULATION_STRATUM: 2720}
    assert not set(frame.candidates["security_key"]) & set(frame.population["security_key"])


def test_a_third_of_the_charts_come_from_candidates():
    assert CANDIDATE_SHARE == pytest.approx(1 / 3)
    drawn = draw(split(bars()), CHARTS_PER_SESSION, seed="s")
    counts = drawn["stratum"].value_counts()
    assert counts[CANDIDATE_STRATUM] == 3
    assert counts[POPULATION_STRATUM] == 7


def test_the_draw_is_the_size_asked_for():
    for count in (5, 10, 25):
        assert len(draw(split(bars()), count, seed="s")) == count


def test_a_day_with_no_candidates_still_draws_charts():
    drawn = draw(split(bars(candidates=0)), CHARTS_PER_SESSION, seed="s")
    assert len(drawn) == 7  # the population share only
    assert set(drawn["stratum"]) == {POPULATION_STRATUM}


# --- weights -----------------------------------------------------------------


def test_the_weights_put_the_population_back_together():
    # The point of inverse-probability weighting: a set that is a third
    # candidates must not be reported as though the world were.
    frame = split(bars())
    drawn = draw(frame, CHARTS_PER_SESSION, seed="s")
    represented = drawn.groupby("stratum")["weight"].sum()
    assert represented[CANDIDATE_STRATUM] == pytest.approx(30)
    assert represented[POPULATION_STRATUM] == pytest.approx(2720)
    assert represented.sum() == pytest.approx(len(bars()))


def test_a_heavily_sampled_stratum_gets_a_small_weight():
    assert inverse_probability_weight(30, 3) == 10
    assert inverse_probability_weight(2720, 7) == pytest.approx(388.57, abs=0.01)


def test_a_stratum_nothing_was_drawn_from_has_no_weight():
    import math

    assert math.isnan(inverse_probability_weight(100, 0))


# --- keeping the trader blind ------------------------------------------------


def test_the_charts_are_mixed_so_their_order_gives_nothing_away():
    # If the three candidates always came first, the trader would work it
    # out within two sessions and the labels would stop being blind.
    drawn = draw(split(bars()), CHARTS_PER_SESSION, seed="s")
    positions = drawn.index[drawn["stratum"] == CANDIDATE_STRATUM].tolist()
    assert positions != [0, 1, 2]


def test_the_drawn_charts_carry_no_hint_of_the_answer():
    drawn = draw(split(bars()), CHARTS_PER_SESSION, seed="s")
    assert "is_candidate" not in drawn.columns


# --- reproducibility ---------------------------------------------------------


def test_the_same_seed_draws_the_same_charts():
    frame = split(bars())
    first = draw(frame, CHARTS_PER_SESSION, seed="2025-03-12")
    again = draw(frame, CHARTS_PER_SESSION, seed="2025-03-12")
    assert list(first["security_key"]) == list(again["security_key"])


def test_a_different_session_draws_different_charts():
    frame = split(bars())
    first = draw(frame, CHARTS_PER_SESSION, seed="2025-03-12")
    later = draw(frame, CHARTS_PER_SESSION, seed="2025-03-13")
    assert list(first["security_key"]) != list(later["security_key"])


def test_the_draw_does_not_depend_on_row_order():
    frame = split(bars())
    first = draw(frame, CHARTS_PER_SESSION, seed="s")
    shuffled = split(bars().sample(frac=1, random_state=3))
    assert set(draw(shuffled, CHARTS_PER_SESSION, seed="s")["security_key"]) == set(
        first["security_key"]
    )


# --- test-retest -------------------------------------------------------------


def shown(count: int, days_ago: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "security_key": f"FIGI_{i:05d}",
                "date": SESSION,
                "slot_index": i % 7,
                "shown_on": SESSION + timedelta(days=90) - timedelta(days=days_ago),
                "is_retest": False,
            }
            for i in range(count)
        ]
    )


def test_a_tenth_of_charts_come_back_for_a_second_look():
    assert RETEST_SHARE == 0.10
    today = SESSION + timedelta(days=90)
    again = due_for_retest(shown(100, days_ago=40), today, seed="s")
    assert len(again) == 10


def test_nothing_comes_back_sooner_than_four_weeks():
    assert RETEST_GAP_DAYS == 28
    today = SESSION + timedelta(days=90)
    assert due_for_retest(shown(100, days_ago=10), today, seed="s").empty


def test_a_retest_is_not_itself_retested():
    today = SESSION + timedelta(days=90)
    already = shown(100, days_ago=40).assign(is_retest=True)
    assert due_for_retest(already, today, seed="s").empty


# --- progress ----------------------------------------------------------------


def test_the_target_is_the_one_section_12_sets():
    assert (TARGET_LABELS, CHARTS_PER_SESSION) == (300, 10)


def test_sessions_remaining_counts_down():
    assert sessions_to_target(0) == 30
    assert sessions_to_target(250) == 5
    assert sessions_to_target(300) == 0
    assert sessions_to_target(400) == 0
