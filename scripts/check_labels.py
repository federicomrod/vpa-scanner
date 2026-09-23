"""How the labelling is going - without giving the answer away.

Run by the project owner, on the mounted volume. The assistant never
runs this and never reads its output file.

### What it will and will not tell you

Two different questions can be asked of a half-finished label set, and
only one of them is safe to ask now.

**"Is the process working?"** - how many labels, how often you say
interesting, whether anything looks mechanical or rushed. Safe, and
worth asking early: a miscalibrated task is much cheaper to fix at 50
than at 300.

**"How is the system doing?"** - how the flagged charts scored against
the rest. **Refused until the target is reached**, and not because the
sample is small, though it is. Knowing at 50 that the flagged charts
are scoring well would sit in your head for the next 250, and the
labels would stop being independent of the system. The sample size
problem cures itself; that one does not.

    uv run python scripts/check_labels.py --labels-dir /Volumes/VPALabels/labels
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from vpa.data.labels import read_labels
from vpa.signal.sampling import CHARTS_PER_SESSION, TARGET_LABELS, sessions_to_target

#: Outside this band the task itself is probably miscalibrated - charts
#: unreadable, or the bar for "interesting" set somewhere odd.
SANE_RATE = (0.08, 0.60)

#: An answer faster than this suggests the chart was not really read.
QUICK_SECONDS = 3


def report(labels: list[dict], unblind: bool) -> None:
    total = len(labels)
    print("\nLabelling progress")
    print("=" * 62)
    if not total:
        print("  No labels yet.")
        return

    print(f"  labels stored          {total:>6}")
    print(f"  still to do            {max(TARGET_LABELS - total, 0):>6}")
    print(f"  sessions of {CHARTS_PER_SESSION} left      "
          f"{sessions_to_target(total):>6}")  # fmt: skip

    answers = Counter(label.get("response", "") for label in labels)
    interesting = answers.get("interesting", 0)
    rate = interesting / total
    print(f"\n  interesting            {interesting:>6}  ({rate:.0%})")
    print(f"  not interesting        {answers.get('not_interesting', 0):>6}")

    print("\nDoes the task look calibrated?")
    print("-" * 62)
    if rate < SANE_RATE[0]:
        print(f"  WORTH A LOOK: {rate:.0%} is low. If almost nothing looks interesting,")
        print("  the charts may be too short to read, or the bar too high.")
    elif rate > SANE_RATE[1]:
        print(f"  WORTH A LOOK: {rate:.0%} is high. If almost everything looks")
        print("  interesting, the labels will not separate anything.")
    else:
        print(f"  {rate:.0%} interesting sits in the usable range "
              f"({SANE_RATE[0]:.0%}-{SANE_RATE[1]:.0%}).")  # fmt: skip

    _runs(labels)
    _pace(labels)
    _extras(labels)
    _duplicates(labels)

    print("\nHow the system is doing")
    print("-" * 62)
    if not unblind:
        print(f"  Not shown. It stays hidden until {TARGET_LABELS} labels are in.")
        print(f"  Not because {total} is too few to measure - though it is - but because")
        print("  knowing the answer now would shape the labels you have not given")
        print("  yet, and their whole value is being independent of the system.")
    elif total < TARGET_LABELS:
        print(f"  --unblind refused: {total} labels, target {TARGET_LABELS}.")
    else:
        _unblinded(labels)


def _runs(labels: list[dict]) -> None:
    longest, current, last = 0, 0, None
    for label in labels:
        answer = label.get("response")
        current = current + 1 if answer == last else 1
        last, longest = answer, max(longest, current)
    if longest >= 12:
        print(f"\n  WORTH A LOOK: {longest} identical answers in a row.")
        print("  That can be real, or it can be a sign of going onto autopilot.")


def _pace(labels: list[dict]) -> None:
    stamps = sorted(
        datetime.fromisoformat(label["answered_at"]) for label in labels if label.get("answered_at")
    )
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(stamps, stamps[1:], strict=False)
        if (b - a).total_seconds() < 600
    ]
    if not gaps:
        return
    typical = sorted(gaps)[len(gaps) // 2]
    quick = sum(1 for gap in gaps if gap < QUICK_SECONDS)
    print(f"\n  typical time per chart  {typical:>5.0f}s")
    if quick > len(gaps) * 0.2:
        print(f"  WORTH A LOOK: {quick} answers came within {QUICK_SECONDS}s of the last.")


def _extras(labels: list[dict]) -> None:
    notes = sum(1 for label in labels if (label.get("note") or "").strip())
    guesses = Counter(label.get("pattern_guess") or "" for label in labels)
    named = {k: v for k, v in guesses.items() if k}
    print(f"\n  notes written          {notes:>6}")
    if named:
        print("  pattern guesses:")
        for guess, count in Counter(named).most_common():
            print(f"    {guess:<28} {count:>4}")


def _duplicates(labels: list[dict]) -> None:
    seen = Counter(
        (label["security_key"], str(label["date"]), int(label["slot_index"])) for label in labels
    )
    repeated = {bar: n for bar, n in seen.items() if n > 1}
    planned = sum(1 for label in labels if label.get("is_retest"))
    if repeated and len(repeated) > planned:
        print(f"\n  WORTH A LOOK: {len(repeated)} charts appear more than once,")
        print(f"  and only {planned} were planned re-tests.")


def _unblinded(labels: list[dict]) -> None:
    by_stratum: dict[str, list[dict]] = {}
    for label in labels:
        by_stratum.setdefault(label.get("stratum", "?"), []).append(label)
    for stratum, rows in sorted(by_stratum.items()):
        hits = sum(1 for row in rows if row.get("response") == "interesting")
        print(f"  {stratum:<12} {hits:>4} of {len(rows):>4} interesting  "
              f"({hits / len(rows):.0%})")  # fmt: skip
    print("\n  Weighted precision belongs in the Section 13.2 analysis, not here.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument(
        "--unblind",
        action="store_true",
        help=f"Show how the flagged charts scored. Refused before {TARGET_LABELS} labels.",
    )
    args = parser.parse_args(argv)
    if not args.labels_dir.exists():
        print(f"{args.labels_dir} is not there. Is the volume mounted?")
        return 1
    report(read_labels(args.labels_dir), args.unblind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
