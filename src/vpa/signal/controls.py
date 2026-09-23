"""Matched controls - Concept v2 Section 11.1.

> For each candidate, sample **5 control ticker-days** from the same date
> and the same universe snapshot, matched on ATR-percentile decile and
> dollar-volume decile, which did **not** trip any filter.

The controls are what make Section 13.1 a real test. Candidates are not
a random slice of the universe - they are, by construction, the busiest
bars in it - so comparing their forward moves against the universe
average would mostly measure that selection. Comparing them against
stocks that were similar in volatility and liquidity on the same day,
and simply did not trip the filter, asks the question that matters:
**did the pattern add anything beyond being a busy, liquid stock?**

### The two axes

- **Dollar volume** is Section 2's own measure, the trailing 60-day
  median, taken from the universe snapshot in force. It is already
  point-in-time, already stored, and constant within a month, so the
  decile cells do not shift underneath the matching.
- **ATR** has no definition anywhere in the specification, and the
  choice matters. Implemented as **ATR as a fraction of price**, which
  is a measure of how volatile a stock is. Raw ATR would mostly sort by
  share price - a $500 stock has a larger ATR than a $50 one at the same
  volatility - and matching on price is not what Section 11.1 is for
  (LEDGER-7, reading 1).

### Five controls do not fit in a decile cell

A 400-stock universe spread over 10 x 10 decile cells holds **four
stocks per cell on average**, and Section 11.1 asks for five. Measured
over 25 sampled dates: mean occupancy 4.1, only 36% of cells hold five
or more, and 55% of the universe sits in a cell that does.

So the match is relaxed by one decile on each axis when a cell is too
thin, which turns a single cell into a 3 x 3 block of roughly forty
stocks - still a close match, and enough every time. How far each match
was relaxed is recorded per control and reported, because "matched
controls" meaning "matched within one decile" for half the sample is
the sort of thing that should be visible rather than buried (LEDGER-7,
reading 2).

The two axes turn out to be nearly independent - rank correlation +0.13
- so the cells fill fairly evenly and this is arithmetic rather than
some quirk of the data.

### Sampling is deterministic

Section 7.3 insists a capped candidate set be reproducible; a control
set has to be too, or every re-run produces slightly different results
and no two analyses can be compared. Controls are therefore chosen by
hashing, not by a random number generator: the same candidate always
draws the same five controls, whatever order the rows arrive in and
whatever else is in the frame.
"""

from __future__ import annotations

import hashlib

import pandas as pd

#: Section 11.1.
CONTROLS_PER_CANDIDATE = 5

DECILES = 10

#: How far the match may be relaxed when a decile cell is too thin, and
#: how that is recorded. An exact match is 0.
MAX_WIDENING = 3

COLUMNS = ["date", "candidate_key", "candidate_slot", "control_key", "widening"]


def deciles(values: pd.Series) -> pd.Series:
    """Cross-sectional deciles, 0 to 9, over one date's universe.

    Ties are broken by first-seen rank so that every cell is the same
    size to within one, even where many stocks share a value.
    """
    usable = values.notna()
    out = pd.Series(pd.NA, index=values.index, dtype="Int64")
    if usable.sum() < DECILES:
        return out
    ranked = values[usable].rank(method="first")
    out.loc[usable] = pd.qcut(ranked, DECILES, labels=False).astype(int)
    return out


def relative_atr(daily_atr: pd.Series, close: pd.Series) -> pd.Series:
    """ATR as a fraction of price - how volatile the stock is, rather
    than how expensive (LEDGER-7, reading 1)."""
    price = close.where(close > 0)
    return daily_atr / price


def match(day: pd.DataFrame, per_candidate: int = CONTROLS_PER_CANDIDATE) -> pd.DataFrame:
    """Controls for every candidate on one session.

    `day` holds one row per universe member for that date, with
    `security_key`, `atr_decile`, `volume_decile`, `is_candidate` and -
    for the candidates - `slot_index`.

    Returns one row per (candidate, control). A candidate that cannot be
    matched at all returns no rows, and the caller is expected to notice
    rather than quietly carry on with fewer controls than it thinks.
    """
    _require(day, ["security_key", "atr_decile", "volume_decile", "is_candidate"])
    eligible = day[~day["is_candidate"].astype(bool)]
    candidates = day[day["is_candidate"].astype(bool)]
    session = day["date"].iloc[0] if "date" in day.columns and len(day) else None

    rows = []
    for candidate in candidates.itertuples(index=False):
        slot = getattr(candidate, "slot_index", 0)
        chosen, widening = _draw(eligible, candidate, per_candidate, session, slot)
        for control in chosen:
            rows.append(
                {
                    "date": session,
                    "candidate_key": candidate.security_key,
                    "candidate_slot": slot,
                    "control_key": control,
                    "widening": widening,
                }
            )
    return pd.DataFrame(rows, columns=COLUMNS)


def _draw(
    eligible: pd.DataFrame, candidate, per_candidate: int, session, slot
) -> tuple[list[str], int]:
    """The nearest `per_candidate` controls, and how far the decile match
    had to be relaxed to find them."""
    if pd.isna(candidate.atr_decile) or pd.isna(candidate.volume_decile):
        return [], -1
    for widening in range(MAX_WIDENING + 1):
        cell = eligible[
            (eligible["atr_decile"] - candidate.atr_decile).abs().le(widening)
            & (eligible["volume_decile"] - candidate.volume_decile).abs().le(widening)
        ]
        if len(cell) >= per_candidate:
            keys = _shuffle(cell["security_key"], session, candidate.security_key, slot)
            return keys[:per_candidate], widening
    return [], -1


def _shuffle(keys: pd.Series, session, candidate_key: str, slot) -> list[str]:
    """Order candidates' controls by hash, so the draw is reproducible.

    Seeded by the candidate as well as the control, so two candidates in
    the same decile cell on the same day do not draw an identical five.
    """
    seed = f"{session}|{candidate_key}|{slot}"
    return sorted(keys, key=lambda key: hashlib.sha1(f"{seed}|{key}".encode()).hexdigest())


def summarise(matched: pd.DataFrame, candidates: int, per_candidate: int) -> dict:
    """How well the matching went, which is worth reporting rather than
    assuming."""
    if matched.empty:
        return {"candidates": candidates, "matched": 0, "unmatched": candidates, "exact": 0}
    per = matched.groupby(["candidate_key", "candidate_slot"]).size()
    return {
        "candidates": candidates,
        "matched": int((per >= per_candidate).sum()),
        "unmatched": candidates - int(per.index.size),
        "exact": int((matched["widening"] == 0).sum()),
        "widened": int((matched["widening"] > 0).sum()),
        "mean_widening": float(matched["widening"].mean()),
    }


def _require(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"control matching needs columns: {missing}")


def attach_deciles(day: pd.DataFrame) -> pd.DataFrame:
    """Add `atr_decile` and `volume_decile` to one date's universe.

    Expects `daily_atr`, `close` and `median_dollar_volume_60`.
    """
    _require(day, ["daily_atr", "close", "median_dollar_volume_60"])
    return day.assign(
        atr_decile=deciles(relative_atr(day["daily_atr"], day["close"])),
        volume_decile=deciles(day["median_dollar_volume_60"]),
    )


def cell_sizes(day: pd.DataFrame) -> pd.Series:
    """How many eligible controls sit in each decile cell.

    A universe of 400 across 100 cells averages 4 per cell, which is
    below the 5 Section 11.1 asks for - so some widening is expected by
    arithmetic, not by accident (LEDGER-7, reading 2).
    """
    eligible = day[~day["is_candidate"].astype(bool)]
    return eligible.groupby(["atr_decile", "volume_decile"], dropna=True).size()


def mean_cell_occupancy(universe_size: int) -> float:
    """How many stocks a decile cell holds on average, by arithmetic."""
    return universe_size / (DECILES * DECILES)
