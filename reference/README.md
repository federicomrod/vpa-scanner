# Reference data committed to the repository

Files here are **facts fetched once from an authoritative source,
checked, and committed**, rather than fetched at run time. The scanner
and the tests read them from disk; nothing here touches the network when
the scanner runs.

## `macro-events.csv`

The dates of the three macro events Concept v2 Section 6 names: FOMC
decision days, CPI releases, and non-farm payrolls (the BLS Employment
Situation release).

| column | meaning |
|---|---|
| `date` | the day the release was published, **not** necessarily the session that traded on it |
| `kind` | `fomc`, `cpi` or `payrolls` |
| `time_et` | publication time, New York time |
| `source` | the page it came from |
| `note` | for FOMC days, whether the meeting was scheduled |

Rebuild it with:

    uv run python -m vpa.data.macro --contact "vpa-scanner you@example.com"

The build refuses to write if a year comes back empty, or if a BLS
release turns up at an unexpected hour - both are signs that a page
changed shape and was parsed wrongly. Genuine calendar oddities are
printed as warnings and kept: 2020 has ten FOMC days rather than eight
(the emergency actions of 3 and 23 March), 2025 is missing one CPI and
one payrolls release (the shutdown), and payrolls moves off its usual
Friday in weeks containing the 4th of July.

Why this is committed rather than fetched: ten years is 350-odd dates,
and a date that is quietly wrong produces a flag that is quietly wrong.
Committing it means the dates are reviewed once, by a person, and then
never change under us. See `docs/ledger/LEDGER-4.md`, amendment 1.
