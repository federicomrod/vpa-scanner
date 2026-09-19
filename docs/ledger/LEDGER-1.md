# LEDGER-1: first implementation of Concept v2 Sections 2-4

**Date opened:** 2026-09-19
**Class:** 2 (touches `src/vpa/signal/`)
**Type:** first implementation of the frozen specification, not a change to it.
No evaluation runs are spent - there is nothing yet to evaluate.

## What

Implement Concept v2 Section 2 (universe), Section 3 (data contract) and
Section 4 (bar construction). The rules and thresholds live in
`src/vpa/signal/`; downloading and file storage live in `src/vpa/data/`.

## Decisions made by the project owner where the specification was silent

1. **Data plan:** Massive/Polygon Starter keeps only 5 years of history,
   which is too short for the Section 11/13 evaluation. Upgrade to
   Developer (10 years) for the historical download; Starter may be
   reconsidered afterwards for the daily top-up.
2. **Screening data:** the whole market is screened using the vendor's
   daily bars. Section 4.2's "our own daily bars are authoritative" still
   applies everywhere else.
3. **Measurement date:** everything is measured at the close of the
   trading day before the rebalance date.
4. **Market cap:** the vendor's figure, as of that previous trading day.
5. **History length:** 250 trading days counted from the vendor's listing
   date.
6. **Prices:** the $10 floor and dollar volume use split-adjusted prices
   (as of the rebalance date), consistent with Section 3.1.

## Readings made during implementation (flagged for review)

- Daily dollar volume = close x volume.
- A trading day with no vendor bar counts as zero dollar volume
  (same principle as Section 4.3).
- A split taking effect on the rebalance date itself is applied (it is
  known before that day's open).
- A stock with no bar on the measurement day has no known close and is
  excluded.
- Ties in dollar volume are broken alphabetically by ticker, so the
  result is always the same.
