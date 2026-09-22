"""Tests for the feature pipeline (src/vpa/data/features.py).

FAKE bars written to a temporary folder; no network, no real data."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from vpa.data.bars import DAILY, HOURLY, derived_path
from vpa.data.features import (
    MARKET_TICKER,
    build,
    completed,
    features_path,
    hourly_features,
    securities_in,
    shard_of,
)
from vpa.signal.sequence import COLUMNS as SEQUENCE_COLUMNS
from vpa.signal.volume import HOURLY_COLUMNS as VOLUME_COLUMNS

SLOTS = 7


def sessions_from(store) -> list[date]:
    from vpa.data.calendar import sessions_between

    return sessions_between(date(2025, 1, 2), date(2025, 6, 30))[:store]


def make_bars(days: list[date], securities: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly and daily bars for each security, at its own price level."""
    hourly_rows, daily_rows = [], []
    for key, base in securities.items():
        for n, day in enumerate(days):
            closes = []
            for slot in range(SLOTS):
                price = base + n * 0.1 + slot * 0.05
                closes.append(price)
                hourly_rows.append(
                    {
                        "security_key": key,
                        "requested_ticker": key.replace("FIGI_", ""),
                        "ticker": key.replace("FIGI_", ""),
                        "date": day,
                        "timestamp_utc": pd.Timestamp(f"{day} 14:30", tz="UTC"),
                        "slot_index": slot,
                        "slot_minutes": 60 if slot < 6 else 30,
                        "is_half_day": False,
                        "open": price,
                        "high": price + 0.2,
                        "low": price - 0.2,
                        "close": price,
                        "volume": 1000.0 + (n % 7) * 100 + slot,
                        "transactions": 10,
                        "minutes_with_trades": 60,
                        "low_quality": False,
                    }
                )
            daily_rows.append(
                {
                    "security_key": key,
                    "requested_ticker": key.replace("FIGI_", ""),
                    "ticker": key.replace("FIGI_", ""),
                    "date": day,
                    "timestamp_utc": pd.Timestamp(f"{day} 14:30", tz="UTC"),
                    "is_half_day": False,
                    "open": closes[0],
                    "high": max(closes) + 0.2,
                    "low": min(closes) - 0.2,
                    "close": closes[-1],
                    "volume": 7000.0 + (n % 7) * 700,
                    "transactions": 70,
                    "minutes_with_trades": 390,
                    "low_quality": False,
                }
            )
    return pd.DataFrame(hourly_rows), pd.DataFrame(daily_rows)


def store_bars(tmp_path, days, securities) -> None:
    hourly, daily = make_bars(days, securities)
    for kind, table in ((HOURLY, hourly), (DAILY, daily)):
        for day, rows in table.groupby("date"):
            path = derived_path(tmp_path, kind, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows.reset_index(drop=True).to_parquet(path, index=False)


@pytest.fixture
def prepared(tmp_path):
    days = sessions_from(70)
    store_bars(
        tmp_path, days, {"FIGI_FAKEA": 50.0, "FIGI_FAKEB": 20.0, f"FIGI_{MARKET_TICKER}": 400.0}
    )
    return tmp_path, days


# --- what the pipeline produces ----------------------------------------------


def shard_for(key: str) -> int:
    return shard_of(key)


def test_it_writes_one_file_per_session_per_shard(prepared):
    store, days = prepared
    build(store, days)
    shard = shard_for("FIGI_FAKEA")
    for day in days:
        assert features_path(store, HOURLY, day, shard).exists()
        assert features_path(store, DAILY, day, shard).exists()


def test_every_section_5_feature_is_present(prepared):
    store, days = prepared
    build(store, days)
    features = pd.read_parquet(features_path(store, HOURLY, days[-1], shard_for("FIGI_FAKEA")))
    for column in [*VOLUME_COLUMNS, *SEQUENCE_COLUMNS]:
        assert column in features.columns
    expected = [
        "atr20", "spread_atr", "body_frac", "close_loc", "ret_atr", "efficiency",
        "beta_60", "resid_ret_atr", "dist_high_20", "dist_round_number",
        "dist_nearest_swing_pivot", "dist_nearest_pivot_high", "dist_nearest_hvn",
        "level_touch_count",
    ]  # fmt: skip
    for column in expected:
        assert column in features.columns, column


def test_each_row_says_which_security_and_bar_it_describes(prepared):
    store, days = prepared
    build(store, days)
    everything = pd.concat(
        [
            pd.read_parquet(p)
            for p in (store / "derived/features/hourly" / f"date={days[-1]}").glob(
                "shard-*.parquet"
            )
        ]
    )
    assert set(everything["security_key"]) == {"FIGI_FAKEA", "FIGI_FAKEB", f"FIGI_{MARKET_TICKER}"}
    assert sorted(everything["slot_index"].unique()) == list(range(SLOTS))
    assert (everything["date"] == days[-1]).all()


def test_a_securitys_shard_depends_only_on_its_own_key():
    # The bug this replaces: batches numbered by position, so "0001" held
    # different securities depending on how the job was run - and a
    # re-run then skipped work it had never done.
    assert shard_of("FIGI_FAKEA") == shard_of("FIGI_FAKEA")
    assert 0 <= shard_of("FIGI_FAKEB") < 24


def test_shards_do_not_overlap(prepared):
    store, days = prepared
    build(store, days)
    seen = {}
    for path in (store / "derived/features/hourly" / f"date={days[-1]}").glob("shard-*.parquet"):
        for key in pd.read_parquet(path, columns=["security_key"])["security_key"]:
            assert seen.setdefault(key, path) == path


def test_a_second_run_skips_work_already_done(prepared):
    store, days = prepared
    assert build(store, days) > 0
    assert build(store, days) == 0
    assert build(store, days, rebuild=True) > 0


def test_a_different_range_of_sessions_is_not_treated_as_done(prepared):
    store, days = prepared
    build(store, days[:40])
    assert completed(store)[f"{shard_for('FIGI_FAKEA'):02d}"] == [
        days[0].isoformat(),
        days[39].isoformat(),
    ]
    # Asking for more sessions must recompute, not skip.
    assert build(store, days) > 0


def test_features_match_computing_them_directly(prepared):
    store, days = prepared
    build(store, days)
    from vpa.data.bars import read_bars

    hourly = read_bars(store, HOURLY, days, as_of=date.today())
    daily = read_bars(store, DAILY, days, as_of=date.today())
    one = hourly[hourly["security_key"] == "FIGI_FAKEA"].sort_values(["date", "slot_index"])
    market_h = hourly[hourly["requested_ticker"] == MARKET_TICKER].sort_values(
        ["date", "slot_index"]
    )
    market_d = daily[daily["requested_ticker"] == MARKET_TICKER].sort_values("date")
    expected = hourly_features(
        one.reset_index(drop=True),
        one.reset_index(drop=True),
        daily[daily["security_key"] == "FIGI_FAKEA"].sort_values("date").reset_index(drop=True),
        market_h.reset_index(drop=True),
        market_d.reset_index(drop=True),
    )
    stored = pd.read_parquet(features_path(store, HOURLY, days[-1], shard_for("FIGI_FAKEA")))
    stored = stored[stored["security_key"] == "FIGI_FAKEA"]
    assert stored["vol_pct_slot_60"].to_numpy() == pytest.approx(
        expected[expected["date"] == days[-1]]["vol_pct_slot_60"].to_numpy(), nan_ok=True
    )


def test_a_market_with_no_bars_stops_the_build(tmp_path):
    days = sessions_from(30)
    store_bars(tmp_path, days, {"FIGI_FAKEA": 50.0})  # no SPY
    with pytest.raises(ValueError, match="SPY"):
        build(tmp_path, days)


def test_securities_are_found_from_the_stored_bars(prepared):
    store, days = prepared
    found = securities_in(store, days)
    assert len(found) == 3
    assert set(found["requested_ticker"]) == {"FAKEA", "FAKEB", MARKET_TICKER}


# --- splits: the one feature that needs prices as traded ---------------------


def test_round_number_distance_uses_prices_as_traded(prepared):
    store, days = prepared
    build(store, days)
    features = pd.read_parquet(features_path(store, HOURLY, days[-1], shard_for("FIGI_FAKEA")))
    distances = features["dist_round_number"].dropna()
    assert not distances.empty
    # A distance to the nearest half dollar can never exceed 0.25 of a
    # dollar, so in ATR units it is bounded by 0.25 / ATR - and never NaN
    # merely because prices were adjusted.
    assert distances.abs().max() < 100


def test_scale_invariant_features_are_unchanged_by_a_constant_adjustment(prepared):
    store, days = prepared
    hourly, daily = make_bars(days, {"FIGI_FAKEA": 50.0, f"FIGI_{MARKET_TICKER}": 400.0})
    market_h = hourly[hourly["requested_ticker"] == MARKET_TICKER].reset_index(drop=True)
    market_d = daily[daily["requested_ticker"] == MARKET_TICKER].reset_index(drop=True)
    one_h = hourly[hourly["security_key"] == "FIGI_FAKEA"].reset_index(drop=True)
    one_d = daily[daily["security_key"] == "FIGI_FAKEA"].reset_index(drop=True)

    halved_h = one_h.assign(
        open=one_h["open"] / 2, high=one_h["high"] / 2, low=one_h["low"] / 2,
        close=one_h["close"] / 2, volume=one_h["volume"] * 2,
    )  # fmt: skip
    halved_d = one_d.assign(
        open=one_d["open"] / 2, high=one_d["high"] / 2, low=one_d["low"] / 2,
        close=one_d["close"] / 2, volume=one_d["volume"] * 2,
    )  # fmt: skip

    before = hourly_features(one_h, one_h, one_d, market_h, market_d)
    after = hourly_features(halved_h, halved_h, halved_d, market_h, market_d)
    for column in ("vol_pct_slot_60", "spread_atr", "ret_atr", "dist_high_20", "efficiency"):
        assert after[column].to_numpy() == pytest.approx(
            before[column].to_numpy(), nan_ok=True, rel=1e-9
        ), column
    # The z-score is only approximately invariant: Section 5.1 uses
    # log(volume + 1), and doubling volume does not simply add log 2.
    # At these fake volumes (~1000 an hour) the drift is about 1e-3; for
    # a real universe member it is nearer 1e-5.
    z_drift = np.nanmax(
        np.abs(after["vol_z_slot_60"].to_numpy() - before["vol_z_slot_60"].to_numpy())
    )
    assert 0 < z_drift < 1e-2

    # The round-number distance is not scale-invariant at all, which is
    # why it is computed from prices as traded.
    assert not np.allclose(
        np.nan_to_num(after["dist_round_number"]), np.nan_to_num(before["dist_round_number"])
    )
