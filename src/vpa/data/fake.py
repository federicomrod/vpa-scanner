"""Fake candidate data for Milestone 1 plumbing tests.

Nothing in this file is real market data, and none of it comes from any
data source - it's hardcoded on purpose. It exists so the rest of the
pipeline (report rendering, delivery) has something to work with before
any real data loading or pattern detection exists. See CLAUDE.md.
"""

from __future__ import annotations

from pydantic import BaseModel

_FAKE_NOTE = "FAKE DATA - Milestone 1 plumbing test, not a real trading signal."


class FakeCandidate(BaseModel):
    """A single fake candidate row. Not a real trading signal."""

    ticker: str
    note: str = _FAKE_NOTE


def generate_fake_candidates() -> list[FakeCandidate]:
    """Return three hardcoded, clearly-fake candidate rows.

    These never come from real market data and never will - this
    function's only job is to prove the pipeline works end to end before
    a real data source and signal logic exist.
    """
    return [
        FakeCandidate(ticker="FAKE1"),
        FakeCandidate(ticker="FAKE2"),
        FakeCandidate(ticker="FAKE3"),
    ]
