"""Applying Section 10's exit rule to candidates and their controls.

This is the machinery Section 13.1 needs. For every candidate bar and
every one of its five matched controls, it answers the same question the
same way: entering at the next open, what did the reference rule do, and
what did the price do at 1, 3, 5 and 10 days?

    derived/outcomes/date=YYYY-MM-DD/outcomes.parquet

Regenerable, like the bars and the features.

### What each row is

One row per **pairing**, not per security. A control matched to three
different candidates on one day appears three times, because the thing
being analysed is the comparison, and a control that stands in for three
candidates carries three times the weight in it. The outcome itself is
computed once per security-session and reused, since Section 10's entry
and exit do not depend on which candidate a control was matched to.

### Both directions, always

Section 10 gives Family B a short bias and calls Family A
direction-agnostic. Rather than decide per row, every signal is walked
long **and** short and both are stored. Controls too - they have to be
comparable with whichever family they are standing in for (LEDGER-6,
reading 1).

### The ATR is the daily one

Section 10's levels are in daily ATR. The hourly feature files carry a
column of the same name holding a different quantity. This module reads
the **daily** feature files for it, and `vpa.signal.exits` names the
parameter `daily_atr` so the mistake is hard to make twice.
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.data.bars import DAILY, HOURLY, derived_path
from vpa.data.events import events_path, stored_sessions
from vpa.data.ingest import setup_logging
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.signal.candidates import candidates
from vpa.signal.controls import CONTROLS_PER_CANDIDATE, attach_deciles, match
from vpa.signal.exits import FORWARD_HORIZONS, LONG, SHORT, TIMEOUT_SESSIONS, forward_moves, walk

log = logging.getLogger("vpa.outcomes")

OUTCOMES = "outcomes"
CANDIDATE, CONTROL = "candidate", "control"

DIRECTIONS = {"long": LONG, "short": SHORT}


def outcomes_path(data_root: Path, session: date) -> Path:
    return data_root / "derived" / OUTCOMES / f"date={session.isoformat()}" / "outcomes.parquet"


# --- reading one session ------------------------------------------------------


def universe_in_force(data_root: Path, session: date) -> pd.DataFrame:
    """The universe snapshot governing a session, with its dollar volume."""
    snapshots = sorted((data_root / "universe").glob("????-??-??.parquet"))
    in_force = [p for p in snapshots if date.fromisoformat(p.stem) <= session]
    if not in_force:
        raise FileNotFoundError(f"No universe snapshot on or before {session}")
    members = pd.read_parquet(
        in_force[-1], columns=["ticker", "composite_figi", "median_dollar_volume_60"]
    )
    members["security_key"] = [
        figi if isinstance(figi, str) and figi else f"TICKER:{ticker}"
        for ticker, figi in zip(members["ticker"], members["composite_figi"], strict=True)
    ]
    return members[["security_key", "median_dollar_volume_60"]]


def hourly_with_prices(data_root: Path, session: date) -> pd.DataFrame:
    """One session's hourly features, joined to the bar prices Section 7
    and Section 10 both need."""
    parts = glob.glob(
        str(data_root / "derived/features/hourly" / f"date={session}" / "shard-*.parquet")
    )
    if not parts:
        raise FileNotFoundError(f"No features for {session}")
    features = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    prices = pd.read_parquet(
        derived_path(data_root, HOURLY, session),
        columns=["security_key", "slot_index", "open", "high", "low", "close"],
    )
    return features.merge(prices, on=["security_key", "slot_index"], how="left")


def daily_atr_by_security(data_root: Path, session: date) -> pd.DataFrame:
    """The **daily** ATR(20) and close for each security on a session."""
    parts = glob.glob(
        str(data_root / "derived/features/daily" / f"date={session}" / "shard-*.parquet")
    )
    frames = [pd.read_parquet(p, columns=["security_key", "atr20"]) for p in parts]
    atr = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    closes = pd.read_parquet(
        derived_path(data_root, DAILY, session), columns=["security_key", "close"]
    )
    return atr.merge(closes, on="security_key", how="inner").rename(columns={"atr20": "daily_atr"})


# --- the forward window -------------------------------------------------------


class Window:
    """The hourly bars after each session, loaded once and reused.

    Each session's bars are needed by the ten sessions before it, so
    they are cached rather than read ten times.
    """

    def __init__(self, data_root: Path, sessions: list[date]):
        self.data_root = data_root
        self.sessions = sessions
        self._cache: dict[date, pd.DataFrame] = {}

    def after(self, session: date) -> dict[str, pd.DataFrame]:
        """Each security's bars over the sessions following `session`,
        ready for `vpa.signal.exits.walk`."""
        at = self.sessions.index(session)
        following = self.sessions[at + 1 : at + 1 + TIMEOUT_SESSIONS]
        frames = [self._load(day) for day in following]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return {}
        bars = pd.concat(frames, ignore_index=True).sort_values(["date", "slot_index"])
        self._evict(before=session)
        return dict(tuple(bars.groupby("security_key", sort=False)))

    def _load(self, day: date) -> pd.DataFrame:
        if day not in self._cache:
            path = derived_path(self.data_root, HOURLY, day)
            self._cache[day] = (
                pd.read_parquet(
                    path,
                    columns=["security_key", "date", "slot_index", "open", "high", "low", "close"],
                )
                if path.exists()
                else pd.DataFrame()
            )
        return self._cache[day]

    def _evict(self, before: date) -> None:
        for day in [d for d in self._cache if d <= before]:
            del self._cache[day]


# --- building -----------------------------------------------------------------


def signals_for(data_root: Path, session: date) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """The session's candidates and their matched controls."""
    members = universe_in_force(data_root, session)
    hourly = hourly_with_prices(data_root, session)
    hourly = hourly[hourly["security_key"].isin(members["security_key"])]
    if hourly.empty:
        return hourly, pd.DataFrame(), {}

    marked = candidates(hourly)
    firing = marked[marked["is_candidate"]]

    daily = daily_atr_by_security(data_root, session).merge(members, on="security_key", how="inner")
    fired_keys = set(firing["security_key"])
    day = attach_deciles(daily.assign(is_candidate=daily["security_key"].isin(fired_keys)))

    # One matching row per candidate bar, so two bars of one stock draw
    # separately - which `vpa.signal.controls` keys on the slot for.
    per_bar = day.drop(columns=["is_candidate"]).merge(
        firing[["security_key", "slot_index"]], on="security_key", how="right"
    )
    frame = pd.concat(
        [
            per_bar.assign(is_candidate=True, date=session),
            day[~day["security_key"].isin(fired_keys)].assign(
                is_candidate=False, slot_index=-1, date=session
            ),
        ],
        ignore_index=True,
    )
    matched = match(frame)
    return firing, matched, {"universe": len(day), "candidates": len(firing)}


def build(data_root: Path, sessions: list[date], rebuild: bool = False) -> int:
    """Compute and store Section 10 outcomes for every candidate and
    control over `sessions`."""
    started = time.monotonic()
    all_sessions = stored_sessions(data_root)
    window = Window(data_root, all_sessions)
    todo = [s for s in sessions if rebuild or not outcomes_path(data_root, s).exists()]
    if not todo:
        log.info("Outcomes already built for all %d sessions", len(sessions))
        return 0

    written = 0
    for n, session in enumerate(todo, 1):
        try:
            rows = session_outcomes(data_root, session, window)
        except FileNotFoundError as exc:
            log.warning("  %s skipped: %s", session, exc)
            continue
        path = outcomes_path(data_root, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows.to_parquet(path, index=False)
        written += len(rows)
        if n % 100 == 0:
            log.info(
                "  %d of %d sessions, %s rows, %.0fs",
                n, len(todo), f"{written:,}", time.monotonic() - started,
            )  # fmt: skip

    log.info(
        "Wrote %s outcome rows over %d sessions in %.0fs",
        f"{written:,}", len(todo), time.monotonic() - started,
    )  # fmt: skip
    return written


def session_outcomes(data_root: Path, session: date, window: Window) -> pd.DataFrame:
    """Every candidate and control outcome for one session."""
    firing, matched, _ = signals_for(data_root, session)
    if firing.empty:
        return pd.DataFrame()

    bars_by_security = window.after(session)
    daily = daily_atr_by_security(data_root, session).set_index("security_key")
    flags = _event_flags(data_root, session)
    cache: dict[str, dict] = {}

    rows = []
    for candidate in firing.itertuples(index=False):
        result = _outcome(candidate.security_key, bars_by_security, daily, cache)
        rows.append(
            {
                "kind": CANDIDATE,
                "security_key": candidate.security_key,
                "requested_ticker": candidate.requested_ticker,
                "date": session,
                "slot_index": int(candidate.slot_index),
                "family_a": bool(candidate.family_a),
                "family_b": bool(candidate.family_b),
                "matched_candidate_key": None,
                "matched_candidate_slot": -1,
                "widening": -1,
                "any_event": flags.get(candidate.security_key),
                **result,
            }
        )

    for control in matched.itertuples(index=False):
        result = _outcome(control.control_key, bars_by_security, daily, cache)
        rows.append(
            {
                "kind": CONTROL,
                "security_key": control.control_key,
                "requested_ticker": None,
                "date": session,
                "slot_index": -1,
                "family_a": False,
                "family_b": False,
                "matched_candidate_key": control.candidate_key,
                "matched_candidate_slot": int(control.candidate_slot),
                "widening": int(control.widening),
                "any_event": flags.get(control.control_key),
                **result,
            }
        )
    return pd.DataFrame(rows)


def _event_flags(data_root: Path, session: date) -> dict:
    path = events_path(data_root, session)
    if not path.exists():
        return {}
    rows = pd.read_parquet(path, columns=["security_key", "any_event"])
    return dict(zip(rows["security_key"], rows["any_event"], strict=True))


def _outcome(key: str, bars_by_security: dict, daily: pd.DataFrame, cache: dict) -> dict:
    """Section 10 applied to one security-session, computed once."""
    if key in cache:
        return cache[key]
    bars = bars_by_security.get(key)
    atr = float(daily["daily_atr"].get(key, float("nan")))
    if bars is None or bars.empty:
        result = {"entry": float("nan"), "daily_atr": atr}
        result.update({f"{n}_{f}": None for n in DIRECTIONS for f in ("reason",)})
        result.update(
            {f"{n}_{f}": float("nan") for n in DIRECTIONS for f in ("r", "move_atr", "sessions")}
        )
        result.update(dict.fromkeys([f"move_{h}d_atr" for h in FORWARD_HORIZONS], float("nan")))
        cache[key] = result
        return result

    entry = float(bars.iloc[0]["open"])
    result = {"entry": entry, "daily_atr": atr}
    for name, direction in DIRECTIONS.items():
        found = walk(bars, entry, atr, direction)
        result[f"{name}_reason"] = found.reason
        result[f"{name}_r"] = found.r_multiple
        result[f"{name}_move_atr"] = found.move_atr
        result[f"{name}_sessions"] = found.sessions_held
    result.update(forward_moves(bars.groupby("date", sort=True)["close"].last(), entry, atr))
    cache[key] = result
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.outcomes",
        description="Apply Section 10's reference exit rule to every candidate and its "
        "Section 11.1 matched controls.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--first-session", type=date.fromisoformat)
    parser.add_argument("--last-session", type=date.fromisoformat)
    parser.add_argument("--every", type=int, default=1, help="Process every Nth session")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "outcomes")
    try:
        sessions = [
            s
            for s in stored_sessions(args.data_root)
            if (args.first_session is None or s >= args.first_session)
            and (args.last_session is None or s <= args.last_session)
        ][:: args.every]
        log.info("Sessions to process: %d (%s to %s)", len(sessions), sessions[0], sessions[-1])
        log.info("Controls per candidate: %d", CONTROLS_PER_CANDIDATE)
        build(args.data_root, sessions, rebuild=args.rebuild)
    except Exception as exc:
        log.exception("OUTCOMES BUILD FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
