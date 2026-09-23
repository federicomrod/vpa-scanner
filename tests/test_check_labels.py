"""Tests for the labelling progress check (scripts/check_labels.py).

Synthetic labels only. Nothing here reads a real label file.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_labels import SANE_RATE, report  # noqa: E402

from vpa.signal.sampling import TARGET_LABELS  # noqa: E402


def labels(count: int, interesting_rate: float = 0.3, **overrides) -> list[dict]:
    start = datetime(2026, 9, 24, 20, 0, 0)
    return [
        {
            "security_key": f"FIGI_{n:03d}",
            "date": "2025-03-10",
            "slot_index": n % 7,
            "stratum": "candidate" if n % 3 == 0 else "population",
            "weight": 500.0,
            "shown_on": "2026-09-24",
            "is_retest": False,
            "response": "interesting" if n < count * interesting_rate else "not_interesting",
            "note": "",
            "pattern_guess": "",
            "answered_at": (start + timedelta(seconds=40 * n)).isoformat(timespec="seconds"),
            **overrides,
        }
        for n in range(count)
    ]


def shown(rows, unblind=False, capsys=None) -> str:
    report(rows, unblind)
    return capsys.readouterr().out


# --- the gate -----------------------------------------------------------------


def test_the_split_is_hidden_before_the_target(capsys):
    out = shown(labels(50), capsys=capsys)
    assert "Not shown" in out
    assert "candidate" not in out.split("How the system is doing")[1]


def test_asking_to_unblind_early_is_refused(capsys):
    out = shown(labels(50), unblind=True, capsys=capsys)
    assert "refused" in out
    assert "50 labels, target 300" in out


def test_the_refusal_says_why_and_it_is_not_sample_size(capsys):
    # The reason that matters: seeing it now would shape the rest.
    out = shown(labels(50), capsys=capsys)
    assert "independent of the system" in out
    assert "not because" in out.lower()


def test_the_split_appears_once_the_target_is_reached(capsys):
    out = shown(labels(TARGET_LABELS), unblind=True, capsys=capsys)
    assert "candidate" in out and "population" in out


def test_reaching_the_target_alone_does_not_unblind(capsys):
    # It still takes asking. Nobody stumbles into it.
    out = shown(labels(TARGET_LABELS), unblind=False, capsys=capsys)
    assert "Not shown" in out


# --- process checks -----------------------------------------------------------


def test_progress_is_counted(capsys):
    out = shown(labels(50), capsys=capsys)
    assert "labels stored              50" in out
    assert "still to do               250" in out


def test_an_odd_interesting_rate_is_flagged(capsys):
    assert "WORTH A LOOK" in shown(labels(50, interesting_rate=0.02), capsys=capsys)
    assert "WORTH A LOOK" in shown(labels(50, interesting_rate=0.95), capsys=capsys)


def test_a_usable_rate_is_not_flagged(capsys):
    out = shown(labels(50, interesting_rate=0.3), capsys=capsys)
    assert "usable range" in out
    assert SANE_RATE[0] < 0.3 < SANE_RATE[1]


def test_a_long_run_of_one_answer_is_flagged(capsys):
    out = shown(labels(50, interesting_rate=0.0), capsys=capsys)
    assert "identical answers in a row" in out


def test_rushing_is_flagged(capsys):
    quick = labels(50)
    start = datetime(2026, 9, 24, 20, 0, 0)
    for n, row in enumerate(quick):
        row["answered_at"] = (start + timedelta(seconds=n)).isoformat(timespec="seconds")
    assert "came within" in shown(quick, capsys=capsys)


def test_an_unplanned_repeat_is_flagged(capsys):
    twice = labels(20) + labels(20)
    assert "appear more than once" in shown(twice, capsys=capsys)


def test_no_labels_yet_is_not_an_error(capsys):
    assert "No labels yet" in shown([], capsys=capsys)
