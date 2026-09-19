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
4. **Market cap:** ~~the vendor's figure, as of that previous trading
   day.~~ Superseded by Amendment 1 below.
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

## Amendment 1 (2026-09-19): point-in-time market cap

**Replaces decision 4.** Decided by the project owner (option B of three).

**What changed:** market cap = the vendor's weighted shares outstanding
as of **105 calendar days before** the measurement day, adjusted for any
split that took effect after that date, x our own previous-day close.
The vendor's own market-cap figure is no longer used.

**Why:** a check against real data (`vpa.data.check_market_cap`, run
2026-09-19, 10 tickers x 3 historical dates) showed the *price* part of
the vendor's figure is point-in-time: 29 of 29 distinguishable cases
matched that day's close to the cent. But the vendor documents that its
*share count* is matched to SEC filings by the period the filing
covers, not the date it was published - so on a given day it can
reflect a filing nobody could yet see. That is look-ahead in universe
selection, which every downstream result inherits. 105 days = the
longest filing deadline (90 days, annual report) + the 15-day
extension, so the filing used was public by the rebalance date.

**Cost accepted:** the share count is 3-6 months stale. Buybacks and new
shares move counts slowly, so the effect near the $2bn/$50bn edges is
small; stale is acceptable, contaminated is not.

**Residual risks, recorded:** a company filing later than even the
extended deadline; and a split between the filing's period end and the
share-count date, which would put the share count on the wrong basis.
The universe build compares its figure against the vendor's and logs
large disagreements for review.

**Options considered and not taken:** (A) keep the vendor's market cap
and accept the lag - rejected, contamination; (C) use SEC EDGAR
publication dates for exact timing - more work and a second data source,
revisit only if the staleness in (B) proves to matter.

## Amendment 2 (2026-09-19): listing date across renames - CONFIRMED

The vendor defines its listing date as when the *symbol* was first
listed, so it may reset when a company changes ticker (e.g. SQ -> XYZ).
The listing date used for the 250-day history rule is therefore the
earlier of the vendor's listing date and the security's first recorded
ticker event.

Confirmed by the project owner: "A rename isn't a new listing, and
treating it as one would exclude long-established companies on a
bookkeeping artifact." Every stock where the two dates differ is logged
(`logs/listing_date_discrepancies.csv`) for audit.

## Amendment 3 (2026-09-19): order of checks

The cheap checks (exchange, type, price, dollar volume) now run before
the history and market-cap checks, so share counts and listing dates are
only looked up for stocks that could still qualify. All checks must
pass either way, so this changes only which check a rejected stock is
counted under - never which stocks are selected.
