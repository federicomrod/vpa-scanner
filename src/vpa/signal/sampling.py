"""Blind label sampling - Concept v2 Section 12.

The blind labels are the only measure in this project that is
independent of both the system and hindsight. Their purpose is to answer
"would a good trader have thought this worth looking at?" without the
trader knowing what the system thought, or what happened next.

Everything here exists to keep that independence intact.

### Stratified, with weights

> **One third** drawn from bar-dates on which some candidate fired
> somewhere in the universe. **Two thirds** drawn uniformly at random
> from universe ticker-days.

Pure uniform sampling would show the trader almost nothing interesting -
candidates are about 1% of bars - and precision could not be measured.
Pure candidate sampling could never reveal what the system **misses**.
Stratifying gives both, and the inverse-probability weights put the
population back together afterwards.

Every drawn bar records its stratum and its weight. **The trader is
never told which stratum a chart came from** - that is the whole point,
and it is why the tool that shows the charts keeps the stratum in a
separate file from the chart itself.

### What a "bar-date" is

Section 12's unit is a bar, not a ticker-day: the chart is "truncated at
the bar close", so the thing being judged is one bar with its history
behind it. A ticker can therefore appear twice on the same date with
different truncation points (LEDGER-8, reading 1).

### The strata do not overlap

Section 12 describes the second stratum as "uniformly at random from
universe ticker-days", which read literally could re-draw a candidate.
Candidates are ~1% of bars so the overlap would be tiny, but overlapping
strata make the weights wrong in a way that is tedious to correct and
easy to get silently wrong. The second stratum is therefore drawn from
**non-candidates only**, which is also what its stated purpose needs -
detecting what the system missed (LEDGER-8, reading 2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pandas as pd

#: Section 12: one third candidates, two thirds everything else.
CANDIDATE_SHARE = 1 / 3

#: Charts per labelling session, and the target Section 12 sets.
CHARTS_PER_SESSION = 10
TARGET_LABELS = 300

#: Section 12's test-retest: a tenth of charts are shown again, no
#: sooner than this many days later.
RETEST_SHARE = 0.10
RETEST_GAP_DAYS = 28

CANDIDATE_STRATUM, POPULATION_STRATUM = "candidate", "population"

COLUMNS = ["security_key", "date", "slot_index", "stratum", "weight"]


@dataclass(frozen=True)
class Frame:
    """The population a draw is made from, and how big each stratum is.

    The sizes are what the inverse-probability weights are built from, so
    they are carried explicitly rather than recomputed later from
    whatever happens to be in memory.
    """

    candidates: pd.DataFrame
    population: pd.DataFrame

    @property
    def sizes(self) -> dict[str, int]:
        return {
            CANDIDATE_STRATUM: len(self.candidates),
            POPULATION_STRATUM: len(self.population),
        }


def split(bars: pd.DataFrame) -> Frame:
    """Divide the eligible bars into the two disjoint strata."""
    _require(bars, ["security_key", "date", "slot_index", "is_candidate"])
    fired = bars["is_candidate"].astype(bool)
    return Frame(candidates=bars[fired], population=bars[~fired])


def draw(frame: Frame, count: int, seed: str) -> pd.DataFrame:
    """`count` bars, stratified, each carrying its stratum and weight.

    `seed` makes the draw reproducible - typically the labelling session
    date. The same seed always produces the same charts, so a session can
    be regenerated without reshuffling what the trader already saw.
    """
    wanted = {
        CANDIDATE_STRATUM: round(count * CANDIDATE_SHARE),
        POPULATION_STRATUM: count - round(count * CANDIDATE_SHARE),
    }
    sizes = frame.sizes
    parts = []
    for stratum, source in (
        (CANDIDATE_STRATUM, frame.candidates),
        (POPULATION_STRATUM, frame.population),
    ):
        taken = _take(source, wanted[stratum], f"{seed}|{stratum}")
        if taken.empty:
            continue
        parts.append(
            taken.assign(
                stratum=stratum,
                weight=inverse_probability_weight(sizes[stratum], len(taken)),
            )
        )
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    drawn = pd.concat(parts, ignore_index=True)
    return _shuffle(drawn, seed)[COLUMNS]


def inverse_probability_weight(population: int, sampled: int) -> float:
    """How many bars in the population each drawn bar stands for.

    A stratum sampled heavily gets a small weight, one sampled sparsely a
    large one, so reweighting reconstructs the population. Without this,
    precision measured on a set that is one-third candidates would be
    reported as though the world were one-third candidates.
    """
    return population / sampled if sampled else float("nan")


def _take(source: pd.DataFrame, count: int, seed: str) -> pd.DataFrame:
    """`count` rows, chosen by hash so the draw never depends on the
    order rows happen to arrive in."""
    if source.empty or count <= 0:
        return source.iloc[:0]
    keyed = sorted(
        range(len(source)),
        key=lambda position: _digest(seed, source.iloc[position]),
    )
    return source.iloc[keyed[:count]].reset_index(drop=True)


def _digest(seed: str, row) -> str:
    return hashlib.sha1(
        f"{seed}|{row['security_key']}|{row['date']}|{row['slot_index']}".encode()
    ).hexdigest()


def _shuffle(drawn: pd.DataFrame, seed: str) -> pd.DataFrame:
    """Mix the strata together so their order gives nothing away."""
    order = sorted(range(len(drawn)), key=lambda p: _digest(f"{seed}|order", drawn.iloc[p]))
    return drawn.iloc[order].reset_index(drop=True)


def due_for_retest(shown: pd.DataFrame, today, seed: str) -> pd.DataFrame:
    """The charts to show a second time, per Section 12's test-retest.

    A tenth of what has been labelled, no sooner than 28 days after it
    was first shown. This measures the trader's agreement with himself,
    which is the **ceiling** on what any model can score against him.
    """
    _require(shown, ["security_key", "date", "slot_index", "shown_on"])
    ripe = shown[
        (pd.to_datetime(shown["shown_on"]).dt.date <= _minus_days(today, RETEST_GAP_DAYS))
        & ~shown.get("is_retest", pd.Series(False, index=shown.index)).astype(bool)
    ]
    return _take(ripe, round(len(shown) * RETEST_SHARE), f"{seed}|retest")


def _minus_days(today, days: int):
    return (pd.Timestamp(today) - pd.Timedelta(days, "D")).date()


def sessions_to_target(labelled: int, per_session: int = CHARTS_PER_SESSION) -> int:
    """How many more labelling sessions the Section 12 target needs."""
    return max(0, -(-(TARGET_LABELS - labelled) // per_session))


def _require(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"sampling needs columns: {missing}")
