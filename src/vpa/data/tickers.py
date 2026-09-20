"""Following a company across ticker changes ("stitching").

The vendor stores price history under the symbol a stock traded under at
the time. When a company renames (FB -> META), its history before the
rename is only available under the old symbol. To get one continuous
history, each security is identified by its Composite FIGI (a permanent
ID that doesn't change with the ticker) and the vendor's ticker-change
events are used to work out which symbol to ask for on which dates.

The vendor's ticker-event data is marked experimental, so this module
never guesses. Every security gets a `status`:

- `ok`: events found and consistent - stitched where needed.
- `no_figi`: the vendor has no permanent ID for it - no stitching possible.
- `no_events`: the vendor has no ticker history for it - no stitching.
- `events_disagree`: the vendor's history says a different symbol was in
  use on the reference date - no stitching, flagged for review.
- `invalid_events`: the vendor's history contains an unusable symbol
  (e.g. blank) - the timeline can't be trusted, so no stitching.

Anything but `ok` falls back to the requested symbol for the whole range
and is reported, so gaps can be audited rather than silently papered over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from vpa.data.massive import MassiveClient, MassiveError

STATUS_OK = "ok"
STATUS_NO_FIGI = "no_figi"
STATUS_NO_EVENTS = "no_events"
STATUS_EVENTS_DISAGREE = "events_disagree"
STATUS_INVALID_EVENTS = "invalid_events"


@dataclass(frozen=True)
class TickerChange:
    """From `date` onwards, the security traded as `ticker`."""

    date: date
    ticker: str


@dataclass(frozen=True)
class Segment:
    """Fetch dates `start`..`end` (inclusive) under symbol `ticker`."""

    ticker: str
    start: date
    end: date


@dataclass(frozen=True)
class Identity:
    """Who a requested symbol really is, and its symbol history."""

    requested: str
    reference_date: date
    composite_figi: str | None
    name: str | None
    status: str
    changes: tuple[TickerChange, ...] = field(default_factory=tuple)

    @property
    def security_key(self) -> str:
        """A stable key for this security in the raw store."""
        return self.composite_figi or f"TICKER:{self.requested}"

    @property
    def all_tickers(self) -> list[str]:
        """Every symbol this security has used (requested one first)."""
        others = [c.ticker for c in self.changes if c.ticker != self.requested]
        return [self.requested, *dict.fromkeys(others)]


def fetch_ticker_events(client: MassiveClient, figi: str) -> list[dict]:
    """The vendor's ticker-change history for a Composite FIGI.

    The vendor answers 404 ("No events found") when it has no history for
    a security. That means "nothing on record" - the security then gets
    status `no_events` and is flagged, not stitched - so it returns [].
    """
    try:
        response = client.get(f"/vX/reference/tickers/{figi}/events", {"types": "ticker_change"})
    except MassiveError as exc:
        if exc.status_code == 404:
            return []
        raise
    return (response.get("results") or {}).get("events") or []


def build_identity(
    requested: str,
    reference_date: date,
    composite_figi: str | None,
    name: str | None,
    events: list[dict],
) -> Identity:
    """Make an Identity from the vendor's ticker-overview and events data."""
    if not composite_figi:
        return Identity(requested, reference_date, None, name, STATUS_NO_FIGI)
    symbols = [
        e.get("ticker_change", {}).get("ticker") for e in events if e.get("type") == "ticker_change"
    ]
    if any(not isinstance(t, str) or not t.strip() for t in symbols):
        # Seen in real data (e.g. Talen Energy): an event with a blank
        # symbol. Dropping it would leave a timeline that silently claims
        # the wrong symbol for that stretch, so the history isn't used.
        return Identity(requested, reference_date, composite_figi, name, STATUS_INVALID_EVENTS)
    changes = tuple(
        sorted(
            (
                TickerChange(date.fromisoformat(e["date"]), e["ticker_change"]["ticker"])
                for e in events
                if e.get("type") == "ticker_change"
            ),
            key=lambda c: c.date,
        )
    )
    if not changes:
        return Identity(requested, reference_date, composite_figi, name, STATUS_NO_EVENTS)
    status = STATUS_OK
    if _ticker_on(changes, reference_date) != requested:
        status = STATUS_EVENTS_DISAGREE
    return Identity(requested, reference_date, composite_figi, name, status, changes)


def segments(identity: Identity, start: date, end: date) -> list[Segment]:
    """Split `start`..`end` into runs of dates sharing one symbol."""
    if start > end:
        return []
    if identity.status != STATUS_OK:
        return [Segment(identity.requested, start, end)]

    boundaries = [c.date for c in identity.changes if start < c.date <= end]
    starts = [start, *boundaries]
    ends = [b - timedelta(days=1) for b in boundaries] + [end]
    result: list[Segment] = []
    for seg_start, seg_end in zip(starts, ends, strict=True):
        ticker = _ticker_on(identity.changes, seg_start)
        if result and result[-1].ticker == ticker:
            result[-1] = Segment(ticker, result[-1].start, seg_end)
        else:
            result.append(Segment(ticker, seg_start, seg_end))
    return result


def _ticker_on(changes: tuple[TickerChange, ...], day: date) -> str:
    """The symbol in use on `day`. Before the first recorded change, the
    first recorded symbol (typically the listing itself)."""
    current = changes[0].ticker
    for change in changes:
        if change.date <= day:
            current = change.ticker
    return current
