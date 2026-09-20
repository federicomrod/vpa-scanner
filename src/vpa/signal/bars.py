"""Hourly and daily bars - Concept v2 Section 4.

Hourly bars are built from 1-minute regular-hours data, anchored to the
session open, giving seven slots on a normal day:

    0: 09:30-10:30   1: 10:30-11:30   2: 11:30-12:30   3: 12:30-13:30
    4: 13:30-14:30   5: 14:30-15:30   6: 15:30-16:00 (30-minute stub)

On a half day (close 13:00 ET) the same rule gives slots 0-2 plus a
30-minute stub. Every bar records `slot_index`, `slot_minutes` and
`is_half_day`, because a 30-minute bar's volume is not comparable with a
60-minute bar's (Section 4.1).

Daily bars are aggregated from regular-hours minutes only, 09:30-16:00 ET.
They differ slightly from the vendor's own daily bars, which include
off-hours trading; ours is the authoritative version (Section 4.2).

Data quality (Section 4.3):

- A minute with no trades produces no vendor bar and counts as zero volume.
- A slot containing fewer than 5 minutes with trades is marked
  `low_quality`; such bars are kept but can never be candidates.
- A slot with no trades at all still produces a bar: zero volume, no
  prices (they would have to be invented), and `low_quality` true.

Prices here are exactly as traded. Split adjustment is applied when bars
are read (`vpa.signal.adjust`), never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd

ET = "America/New_York"

#: Regular trading hours (Section 3.2).
SESSION_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)

#: Hourly slots are this long, except the stub at the end of the session.
SLOT_MINUTES = 60

#: Fewer minutes with trades than this in a slot marks it low quality.
MIN_MINUTES_WITH_TRADES = 5

MINUTE_COLUMNS = ["security_key", "requested_ticker", "ticker", "timestamp_utc", "open", "high",
                  "low", "close", "volume", "transactions"]  # fmt: skip


@dataclass(frozen=True)
class Slot:
    """One hourly slot of a session."""

    index: int
    start: pd.Timestamp  # ET
    end: pd.Timestamp  # ET, exclusive
    minutes: int


def session_slots(session_open: pd.Timestamp, session_close: pd.Timestamp) -> list[Slot]:
    """The slots of one session, anchored to the open (Section 4.1)."""
    slots = []
    start = session_open
    while start < session_close:
        end = min(start + timedelta(minutes=SLOT_MINUTES), session_close)
        slots.append(Slot(len(slots), start, end, int((end - start).total_seconds() // 60)))
        start = end
    return slots


def is_half_day(session_close: pd.Timestamp) -> bool:
    return session_close.time() == HALF_DAY_CLOSE


def hourly_bars(
    minutes: pd.DataFrame, session_open: pd.Timestamp, session_close: pd.Timestamp
) -> pd.DataFrame:
    """Hourly bars for one session, one row per security per slot."""
    slots = session_slots(session_open, session_close)
    regular = _regular_hours(minutes, session_open, session_close)
    edges = [s.start for s in slots] + [session_close]
    regular = regular.assign(
        slot_index=pd.cut(regular["et"], bins=edges, right=False, labels=False).astype("Int64")
    )

    securities = _identities(minutes)
    grid = securities.merge(
        pd.DataFrame({"slot_index": range(len(slots)),
                      "slot_minutes": [s.minutes for s in slots],
                      "timestamp_utc": [s.start.tz_convert("UTC") for s in slots]}),
        how="cross",
    )  # fmt: skip
    bars = _aggregate(regular, ["security_key", "slot_index"])
    bars = grid.merge(bars, on=["security_key", "slot_index"], how="left")
    return _finish(bars, session_open, session_close)[_BAR_COLUMNS + ["slot_index", "slot_minutes"]]


def daily_bars(
    minutes: pd.DataFrame, session_open: pd.Timestamp, session_close: pd.Timestamp
) -> pd.DataFrame:
    """One daily bar per security, from regular-hours minutes only."""
    regular = _regular_hours(minutes, session_open, session_close)
    bars = _identities(minutes).merge(
        _aggregate(regular, ["security_key"]), on="security_key", how="left"
    )
    bars["timestamp_utc"] = session_open.tz_convert("UTC")
    return _finish(bars, session_open, session_close)[_BAR_COLUMNS]


_BAR_COLUMNS = ["security_key", "requested_ticker", "ticker", "date", "timestamp_utc",
                "is_half_day", "open", "high", "low", "close", "volume", "transactions",
                "minutes_with_trades", "low_quality"]  # fmt: skip


def _regular_hours(
    minutes: pd.DataFrame, session_open: pd.Timestamp, session_close: pd.Timestamp
) -> pd.DataFrame:
    """Only the minutes inside regular trading hours, in time order;
    pre-market and after-hours are stored but never used (Section 3.2)."""
    missing = set(MINUTE_COLUMNS) - set(minutes.columns)
    if missing:
        raise ValueError(f"minutes table is missing columns: {sorted(missing)}")
    et = pd.to_datetime(minutes["timestamp_utc"], utc=True).dt.tz_convert(ET)
    inside = (et >= session_open) & (et < session_close)
    return minutes.assign(et=et)[inside].sort_values("et")


def _identities(minutes: pd.DataFrame) -> pd.DataFrame:
    """Every security with data in this session, including pre-market only."""
    identity = ["security_key", "requested_ticker", "ticker"]
    return minutes[identity].drop_duplicates().sort_values(identity).reset_index(drop=True)


def _aggregate(regular: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Open/high/low/close/volume per group, from minutes in time order."""
    if regular.empty:
        return pd.DataFrame(columns=[*keys, "open", "high", "low", "close", "volume",
                                     "transactions", "minutes_with_trades"])  # fmt: skip
    return (
        regular.groupby(keys, observed=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            transactions=("transactions", "sum"),
            minutes_with_trades=("close", "size"),
        )
        .reset_index()
    )


def _finish(bars: pd.DataFrame, session_open: pd.Timestamp, session_close: pd.Timestamp):
    """Fill in the no-trades case and the per-session columns."""
    bars["date"] = session_open.date()
    bars["is_half_day"] = is_half_day(session_close)
    counts = {"volume": float, "transactions": int, "minutes_with_trades": int}
    for column, kind in counts.items():
        bars[column] = pd.to_numeric(bars[column], errors="coerce").fillna(0).astype(kind)
    bars["low_quality"] = bars["minutes_with_trades"] < MIN_MINUTES_WITH_TRADES
    return bars


def session_bounds(session: pd.Timestamp | datetime, close: time) -> tuple[pd.Timestamp, ...]:
    """ET open and close timestamps for a session, given its close time."""
    day = pd.Timestamp(session).date()
    return (
        pd.Timestamp(datetime.combine(day, SESSION_OPEN), tz=ET),
        pd.Timestamp(datetime.combine(day, close), tz=ET),
    )
