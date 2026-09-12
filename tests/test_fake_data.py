"""Tests for src/vpa/data/fake.py. No network calls."""

from vpa.data.fake import generate_fake_candidates


def test_generates_exactly_three_candidates():
    candidates = generate_fake_candidates()

    assert len(candidates) == 3


def test_every_candidate_is_clearly_labelled_fake():
    candidates = generate_fake_candidates()

    for candidate in candidates:
        assert "FAKE" in candidate.note.upper()


def test_tickers_are_unique():
    candidates = generate_fake_candidates()

    tickers = [c.ticker for c in candidates]
    assert len(tickers) == len(set(tickers))


def test_calling_twice_returns_independent_lists():
    first_call = generate_fake_candidates()
    second_call = generate_fake_candidates()

    first_call.pop()

    assert len(second_call) == 3
