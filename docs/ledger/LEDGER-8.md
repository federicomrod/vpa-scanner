# LEDGER-8: Section 12 blind label sampling

**Date:** 2026-09-23
**Class:** new code in the frozen area (`vpa.signal.sampling`),
implementing Section 12's stratified draw. The tool that renders and
serves the charts follows separately; this is the rule that decides
which bars the trader sees.

## Why this is the long pole

Section 12 targets 300+ labels at 10 charts a session: about 30
sessions, roughly four months of the project owner's time, plus a
28-day wait before the test-retest subset can begin. LEDGER-3 identified
it as the only calendar-bound item in the project. It is built now, well
ahead of the analysis that consumes it, so the labelling can run in
parallel with everything else.

## Reading 1: the unit is a bar

Section 12 says the chart is "truncated at the bar close", so the thing
being judged is one bar with its history behind it - not a ticker-day. A
ticker can appear twice on the same date with different truncation
points, and the two are different questions.

## Reading 2: the strata do not overlap

Section 12's second stratum is "uniformly at random from universe
ticker-days", which read literally could re-draw a bar that is already
in the candidate stratum. Candidates are about 1% of bars, so the
overlap would be tiny - but overlapping strata make inverse-probability
weights wrong in a way that is tedious to correct and easy to get
silently wrong.

The second stratum is therefore drawn from **non-candidates only**,
which is also what its stated purpose requires: "pure candidate sampling
would make it impossible to detect what the system misses."

## What keeps the labels blind

Three things, each with a test:

- **The drawn charts carry no `is_candidate` column.** The stratum and
  weight are recorded, but the tool that shows the charts keeps them
  apart from the chart itself.
- **The strata are mixed before presentation.** If the three candidates
  always came first, the trader would work it out within two sessions
  and the labels would stop being blind.
- **The draw is reproducible from a seed**, so a session can be
  regenerated without reshuffling what the trader has already seen.

## The weights, and why they matter

A session of 10 charts is 3 candidates and 7 others. Candidates are ~1%
of the population. Reporting precision on that set without reweighting
would describe a world that is one-third candidates.

Each drawn bar therefore carries how many bars of its stratum it stands
for. Checked: over a day of 2,750 bars with 30 candidates, the drawn
weights sum to 30 and 2,720 - reconstructing the population exactly.

## How the rules were checked

Eight deliberate breaks, each confirmed to fail the tests:

| Break | Caught |
|---|---|
| Two thirds candidates instead of one third | yes (2 tests) |
| The strata allowed to overlap | yes (2) |
| All weights equal, so the population is not reconstructed | yes (3) |
| Charts not mixed, candidates shown first | yes (1) |
| The 28-day retest gap removed | yes (1) |
| Half the charts retested rather than a tenth | yes (1) |
| Retests themselves retested | yes (1) |
| The draw leaks the `is_candidate` column | yes (1) |

**A note on the method.** The first run of these reported one break as
missed. It had not been applied at all - the pattern failed to match, so
the tests passed because nothing had changed. The harness now compares
the file before and after and reports a non-matching pattern as `NO-OP`
rather than letting it masquerade as a missed mutation. Every earlier
mutation table in this ledger was re-checked against the same risk where
the pattern was non-trivial.

## Still open

- Where the labels are stored. Section 12 puts them in the holdout
  directory, outside the repository, which this assistant must never
  read (CLAUDE.md rule 3, Section 11.4). The tool will take the path as
  a required argument with no default, so it is supplied by the project
  owner and never hardcoded here.
