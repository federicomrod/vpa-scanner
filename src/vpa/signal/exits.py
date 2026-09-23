"""The reference exit rule - Concept v2 Section 10.

**This is a measuring stick, not a strategy.** Section 10 is emphatic:
it "exists only to make signals comparable... It is not a trading
strategy and must never be optimised. Frozen permanently."

Nothing here places an order or talks to a broker, and nothing ever
will. It reads historical bars and answers one question: if you had
treated this signal the same way you treat every other signal, what
would have happened? The point is that the answer is computed
identically for candidates, for controls and for both baselines, so the
comparison between them means something.

### The rule

| | |
|---|---|
| entry | the open of the next trading day after the signal |
| stop | 1.5 x daily ATR(20) adverse |
| target | 3.0 x daily ATR(20) |
| timeout | 10 trading days, exit at the close |

The ATR is the one in force on the signal's own session - strictly
trailing, as Section 5 defines it - so the levels are knowable at the
moment the signal fires and never move afterwards.

**It must be the DAILY ATR**, and the parameter is named `daily_atr` to
say so at every call site. The stored hourly features also carry a
column called `atr20`, and it is a different quantity: the ATR of the
*hourly* bar sequence, which Section 5.2 uses for `spread_atr`. On ANF's
gap day the two were 7.996 and 3.719. Passing the hourly one silently
halves every stop and target, and nothing about the output looks wrong
(LEDGER-6, reading 4). The daily ATR lives in the daily feature files.

**Direction.** Section 10 gives Family B a short bias and calls Family A
"direction-agnostic", whose "primary metric is the **absolute**
ATR-normalised move". An exit rule needs a direction that an absolute
move does not have, so Family A is walked **both ways** and both results
are kept (LEDGER-6, reading 1). Nothing downstream has to choose, and
Section 13.1 - the criterion this all feeds - asks for the absolute
forward move rather than an R-multiple.

### What the specification does not settle, and what was done

Three gaps, each recorded in LEDGER-6 rather than decided quietly:

- **A bar that touches both levels.** The stop is taken first. It is the
  pessimistic reading, and for a measuring stick a consistent pessimism
  is much safer than an optimism that flatters every signal equally.
  Hourly bars are walked rather than daily ones, which removes most of
  these: a day that touches both levels usually does so in different
  hours.
- **A gap through a level.** The fill is the bar's open when the bar
  opened beyond the level, otherwise the level itself. That cuts both
  ways - a gap through the stop fills worse, a gap through the target
  fills better - and matches what actually happens.
- **A security that stops trading mid-window** - acquired, delisted -
  exits at its last close, and says so. Silently treating that as a
  timeout would make an acquisition look like a flat result.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Section 10, frozen permanently.
STOP_ATR = 1.5
TARGET_ATR = 3.0
TIMEOUT_SESSIONS = 10

#: Extra horizons recorded alongside, "independent of the exit rule".
FORWARD_HORIZONS = (1, 3, 5, 10)

LONG, SHORT = 1, -1

#: Why a position ended.
STOPPED, TARGET, TIMEOUT, DELISTED, NO_ENTRY = (
    "stop",
    "target",
    "timeout",
    "delisted",
    "no_entry",
)


@dataclass(frozen=True)
class Outcome:
    """What the reference rule would have done with one signal."""

    reason: str
    entry: float
    exit: float
    sessions_held: int
    #: Move in the direction taken, in ATR. Signed: negative is adverse.
    move_atr: float
    #: The same move in units of the initial risk. The target is +2R and
    #: the stop -1R by construction.
    r_multiple: float

    @property
    def resolved(self) -> bool:
        """Whether the rule reached an end it can speak for."""
        return self.reason in (STOPPED, TARGET, TIMEOUT)


def levels(entry: float, daily_atr: float, direction: int) -> tuple[float, float]:
    """The stop and target prices for a position taken at `entry`."""
    return (
        entry - direction * STOP_ATR * daily_atr,
        entry + direction * TARGET_ATR * daily_atr,
    )


def walk(bars: pd.DataFrame, entry: float, daily_atr: float, direction: int) -> Outcome:
    """Walk one signal's bars until a level is hit or the clock runs out.

    `bars` is the security's hourly bars from the entry session onward,
    in order, each carrying `date`, `open`, `high`, `low` and `close`.
    They are walked one at a time, so nothing about a later bar can
    decide an earlier one's outcome.
    """
    if bars.empty or not np.isfinite(entry) or not np.isfinite(daily_atr) or daily_atr <= 0:
        return Outcome(NO_ENTRY, entry, np.nan, 0, np.nan, np.nan)

    stop, target = levels(entry, daily_atr, direction)
    sessions = list(dict.fromkeys(bars["date"]))[:TIMEOUT_SESSIONS]
    window = bars[bars["date"].isin(sessions)]

    for bar in window.itertuples(index=False):
        held = sessions.index(bar.date) + 1
        # The stop is checked first, so a bar that reaches both levels is
        # read pessimistically - the same way every time.
        falling_to_stop = direction == LONG
        if _reached(bar, stop, falling_to_stop):
            return _outcome(
                STOPPED, entry, _fill(bar, stop, falling_to_stop), held, daily_atr, direction
            )
        if _reached(bar, target, not falling_to_stop):
            return _outcome(
                TARGET, entry, _fill(bar, target, not falling_to_stop), held, daily_atr, direction
            )

    # Neither level was reached. Either the clock ran out, or the bars
    # did - a security that was acquired or delisted mid-window, which is
    # not the same thing and must not be recorded as though it were.
    last = window.iloc[-1]
    ran_full_term = len(sessions) >= TIMEOUT_SESSIONS
    reason = TIMEOUT if ran_full_term else DELISTED
    return _outcome(reason, entry, last["close"], len(sessions), daily_atr, direction)


def _reached(bar, level: float, going_down: bool) -> bool:
    """Whether a bar traded through a level, from the side it is on."""
    return bar.low <= level if going_down else bar.high >= level


def _fill(bar, level: float, going_down: bool) -> float:
    """The price the exit would have got.

    A bar that opened beyond the level fills at its open - the level was
    already gone before trading began. That cuts both ways: a gap through
    the stop fills worse, a gap through the target fills better.
    """
    return min(bar.open, level) if going_down else max(bar.open, level)


def _outcome(
    reason: str, entry: float, exit_price: float, held: int, daily_atr: float, direction: int
) -> Outcome:
    move = direction * (exit_price - entry)
    return Outcome(reason, entry, exit_price, held, move / daily_atr, move / (STOP_ATR * daily_atr))


def forward_moves(closes: pd.Series, entry: float, daily_atr: float) -> dict[str, float]:
    """The ATR-normalised move at each Section 10 horizon.

    Recorded "independent of the exit rule": these ignore the stop, the
    target and the timeout entirely, and say what the price did. They are
    **signed and measured from the entry**, so a candidate and a control
    sampled on the same date are measured from the same kind of moment.
    """
    moves = {}
    for horizon in FORWARD_HORIZONS:
        if len(closes) >= horizon and np.isfinite(daily_atr) and daily_atr > 0:
            moves[f"move_{horizon}d_atr"] = (closes.iloc[horizon - 1] - entry) / daily_atr
        else:
            moves[f"move_{horizon}d_atr"] = np.nan
    return moves
