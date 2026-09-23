"""Section 13.1: does the deterministic filter earn its place?

> Retired unless the candidate set's mean absolute 5-day ATR-normalised
> move exceeds matched controls by **>= 0.15 ATR**, at **p < 0.01** with
> date-clustered standard errors, holding in **>= 2 of 3** walk-forward
> folds.

Section 13.4 is worth reading before the output: "If the criteria are not
met, the pattern family is retired and documented. **This is a
successful outcome of the project, not a failure.**"

### Match quality is reported first, on purpose

Section 11.1 asks for controls from the same volatility and liquidity
decile. A 400-stock universe cannot supply five of those, so about half
are matched within one decile instead (LEDGER-7, reading 2). A headline
difference of "+0.20 ATR, p < 0.001" would look equally clean whether
the controls were tightly or loosely matched, so the match quality is
printed **above** the result rather than in a footnote, at the project
owner's request.

### The holdout is not touched

Section 11.4 reserves the final 12 months, "examined exactly once, at
the end", and puts it outside the reach of this assistant. This script
refuses to read anything on or after `HOLDOUT_FROM`. The holdout
evaluation is the project owner's to run.

Run it with `uv run python scripts/check_kill_criterion.py --help`.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from vpa.data.events import stored_sessions
from vpa.data.outcomes import CANDIDATE, CONTROL, outcomes_path
from vpa.data.raw_store import DEFAULT_DATA_ROOT

#: Section 13.1.
REQUIRED_EDGE_ATR = 0.15
REQUIRED_P = 0.01
HORIZON = "move_5d_atr"

#: Section 13.3, the gate before any criterion may be applied.
MIN_EFFECTIVE_OCCURRENCES = 250
MIN_YEARS = 3

#: Section 13.1 says "in >= 2 of 3 walk-forward folds", so three folds.
#: Section 11.4's 18-month train / 6-month test / 3-month purge comes to
#: 27 months a fold, which over the development window is about three -
#: the two readings agree on the count.
#:
#: Nothing is fitted for this criterion, so the training portion has no
#: work to do: the development window is split into three equal test
#: windows separated by the purge gap.
FOLDS = 3
PURGE_MONTHS = 3
REQUIRED_FOLD_SHARE = 2 / 3

#: Section 11.4's final 12-month holdout. Nothing here reads on or after
#: this date; the owner runs that evaluation, once, at the end.
HOLDOUT_FROM = date(2025, 9, 19)

#: Feature warm-up, before which the store cannot produce candidates.
WARMUP_SESSIONS = 120


def load(data_root: Path, sessions: list[date]) -> pd.DataFrame:
    frames = []
    for session in sessions:
        path = outcomes_path(data_root, session)
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise FileNotFoundError("No outcomes stored. Run `python -m vpa.data.outcomes` first.")
    return pd.concat(frames, ignore_index=True)


def clustered_difference(rows: pd.DataFrame, families: list[str]) -> dict:
    """Mean absolute 5-day move, candidates against their own controls.

    Standard errors are **clustered by date**: candidates on one day are
    not independent of each other, and treating them as though they were
    would shrink the error bars by roughly the square root of the number
    of candidates a day, which is where a spurious p-value would come
    from (Section 11.4).

    The estimate is the mean over dates of (candidate mean - control
    mean) on that date, so each day counts once however many candidates
    it produced.
    """
    per_date = []
    for session, day in rows.groupby("date"):
        picked = day[(day["kind"] == CANDIDATE) & day[families].any(axis=1)]
        if picked.empty:
            continue
        keys = set(zip(picked["security_key"], picked["slot_index"], strict=True))
        pairs_a_picked_candidate = np.array(
            [
                (k, s) in keys
                for k, s in zip(
                    day["matched_candidate_key"], day["matched_candidate_slot"], strict=True
                )
            ]
        )
        controls = day[(day["kind"] == CONTROL).to_numpy() & pairs_a_picked_candidate]
        moves = picked[HORIZON].abs().dropna()
        against = controls[HORIZON].abs().dropna()
        if moves.empty or against.empty:
            continue
        per_date.append(
            {
                "date": session,
                "candidates": len(moves),
                "controls": len(against),
                "candidate_mean": moves.mean(),
                "control_mean": against.mean(),
                "difference": moves.mean() - against.mean(),
            }
        )

    daily = pd.DataFrame(per_date)
    if daily.empty:
        return {"dates": 0, "edge": float("nan"), "p": float("nan")}
    differences = daily["difference"]
    clusters = len(differences)
    edge = differences.mean()
    standard_error = differences.std(ddof=1) / math.sqrt(clusters) if clusters > 1 else float("nan")
    t = edge / standard_error if standard_error else float("nan")
    return {
        "dates": clusters,
        "candidates": int(daily["candidates"].sum()),
        "controls": int(daily["controls"].sum()),
        "candidate_mean": daily["candidate_mean"].mean(),
        "control_mean": daily["control_mean"].mean(),
        "edge": edge,
        "standard_error": standard_error,
        "t": t,
        "p": two_sided_p(t),
        "daily": daily,
    }


def two_sided_p(t: float) -> float:
    """A normal approximation, which is what thousands of date clusters
    permit. Stated rather than assumed: with fewer than about 100 dates
    this would understate the p-value."""
    if not np.isfinite(t):
        return float("nan")
    return math.erfc(abs(t) / math.sqrt(2))


def folds(sessions: list[date]) -> list[tuple[date, date]]:
    """Three equal test windows, separated by the purge gap.

    An earlier version cut the window into twelve six-month folds, which
    looked more thorough and was worse: each fold held a twelfth of the
    data, so its standard error was about three and a half times the
    aggregate's and no fold could reach p < 0.01 whatever the effect.
    That is a property of the fold size, not of the signal (LEDGER-9).
    """
    if not sessions:
        return []
    purge = timedelta(days=30 * PURGE_MONTHS)
    span = (sessions[-1] - sessions[0] - purge * (FOLDS - 1)) / FOLDS
    out, cursor = [], sessions[0]
    for _ in range(FOLDS):
        out.append((cursor, cursor + span))
        cursor = cursor + span + purge
    return out


def match_quality(rows: pd.DataFrame) -> dict:
    """How tightly the controls were matched (LEDGER-7, reading 2)."""
    controls = rows[rows["kind"] == CONTROL]
    if controls.empty:
        return {}
    widening = controls["widening"]
    return {
        "controls": len(controls),
        "exact": int((widening == 0).sum()),
        "within_one": int((widening == 1).sum()),
        "within_two_or_more": int((widening >= 2).sum()),
        "mean": float(widening.mean()),
    }


def report(data_root: Path, families: list[str]) -> None:
    sessions = stored_sessions(data_root)
    usable = [s for s in sessions[WARMUP_SESSIONS:] if s < HOLDOUT_FROM]
    rows = load(data_root, usable)
    rows = rows[rows["date"] < HOLDOUT_FROM]  # belt and braces

    label = " and ".join(f.replace("family_", "Family ").upper() for f in families)
    print(f"\nSection 13.1 - does the deterministic filter earn its place?  [{label}]")
    print("=" * 74)
    print(f"development window: {usable[0]} to {usable[-1]} "
          f"({(usable[-1] - usable[0]).days / 365.25:.1f} years, holdout excluded)")  # fmt: skip

    quality = match_quality(rows)
    print("\nHOW WELL THE CONTROLS WERE MATCHED")
    print("-" * 74)
    if quality:
        total = quality["controls"]
        print(f"  controls drawn:                  {total:>9,}")
        print(f"  matched in the same decile cell: {quality['exact']:>9,}  "
              f"({100 * quality['exact'] / total:5.1f}%)")  # fmt: skip
        print(f"  matched within one decile:       {quality['within_one']:>9,}  "
              f"({100 * quality['within_one'] / total:5.1f}%)")  # fmt: skip
        print(f"  matched two deciles out or more: {quality['within_two_or_more']:>9,}  "
              f"({100 * quality['within_two_or_more'] / total:5.1f}%)")  # fmt: skip
        print(f"  average widening:                {quality['mean']:>9.2f} deciles")
        print("\n  A 400-stock universe holds ~4 stocks per decile cell and Section 11.1")
        print("  asks for 5, so widening is arithmetic rather than accident (LEDGER-7).")

    found = clustered_difference(rows, families)
    print("\nTHE COMPARISON")
    print("-" * 74)
    if not found["dates"]:
        print("  No candidate had a usable 5-day forward move. Nothing to report.")
        return
    print(f"  dates (clusters):                {found['dates']:>9,}")
    print(f"  candidate bars:                  {found['candidates']:>9,}")
    print(f"  control ticker-days:             {found['controls']:>9,}")
    print(f"\n  mean |5-day move|, candidates:   {found['candidate_mean']:>9.4f} ATR")
    print(f"  mean |5-day move|, controls:     {found['control_mean']:>9.4f} ATR")
    print(f"  difference (the edge):           {found['edge']:>+9.4f} ATR")
    print(f"  standard error (date-clustered): {found['standard_error']:>9.4f}")
    print(f"  t:                               {found['t']:>9.2f}")
    print(f"  p (two-sided, normal approx.):   {found['p']:>9.2e}")

    print("\nWALK-FORWARD STABILITY")
    print("-" * 74)
    windows = folds(usable)
    strict = effect_only = 0
    print('  Two readings of "holding in a fold", because Section 13.1 does not say')
    print("  whether the whole criterion or only the effect must hold per fold:")
    print()
    header = f"  {'window':<26} {'dates':>6} {'edge':>9} {'p':>9}"
    print(f"{header}   {'edge>=.15':>10} {'and p<.01':>10}")
    for start, end in windows:
        part = rows[(rows["date"] >= start) & (rows["date"] < end)]
        fold = clustered_difference(part, families)
        big = fold["edge"] >= REQUIRED_EDGE_ATR
        both = big and fold["p"] < REQUIRED_P
        strict += both
        effect_only += big
        print(f"  {start} to {end} {fold['dates']:>6,} {fold['edge']:>+9.4f} {fold['p']:>9.1e}"
              f"   {'PASS' if big else 'fail':>10} {'PASS' if both else 'fail':>11}")  # fmt: skip
    total = len(windows)
    print(f"\n  folds where the effect holds:        {effect_only} of {total}"
          f"  ({effect_only / total:.0%})")  # fmt: skip
    print(f"  folds where the whole criterion holds: {strict} of {total}"
          f"  ({strict / total:.0%})")  # fmt: skip
    print(f"  Section 11.4 asks for {REQUIRED_FOLD_SHARE:.0%}")
    share = effect_only / total if total else 0

    print("\nVERDICT")
    print("-" * 74)
    checks = [
        (f"edge >= {REQUIRED_EDGE_ATR} ATR", found["edge"] >= REQUIRED_EDGE_ATR),
        (f"p < {REQUIRED_P}", found["p"] < REQUIRED_P),
        (f"holds in >= {REQUIRED_FOLD_SHARE:.0%} of folds", share >= REQUIRED_FOLD_SHARE),
    ]
    for name, ok in checks:
        print(f"  {name:<36} {'PASS' if ok else 'FAIL'}")
    print(f"\n  {'The filter earns its place.' if all(o for _, o in checks) else
             'Section 13.1 not met. Section 13.4: retiring a family on this evidence is a '
             'successful outcome, not a failure.'}")  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--family", choices=["a", "b", "both"], default="both")
    args = parser.parse_args(argv)
    families = {"a": ["family_a"], "b": ["family_b"], "both": ["family_a", "family_b"]}[args.family]
    report(args.data_root, families)
    return 0


if __name__ == "__main__":
    sys.exit(main())
