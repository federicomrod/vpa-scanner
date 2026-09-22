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

**Superseded in part by Amendment 4: the weighted count is replaced by
the share-class count. The 105-day lag below still stands.**

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

## Amendment 4 (2026-09-20): share-class count, not the weighted count

**Supersedes the share-count half of Amendment 1.** Decided by the
project owner (option A of three), with both counts now stored.

**What changed:** market cap = the vendor's **share-class** shares
outstanding as of the share-count date (still 105 calendar days back),
brought forward through any split since, x our previous-day close.
Amendment 1's *weighted* share count is no longer used for selection; it
is still downloaded and stored, for audit.

**Why - the evidence** (`vpa.data.check_share_counts --history`, run
2026-09-20 on A, WSM, CROX and ALXN across 2016-2026):

| | weighted count, 2016-2021 | share-class count, same dates |
|---|---|---|
| A | 302,000,797 on all 7 dates | 324.4m -> 303.4m, moving |
| WSM | 72,954,519 on all 7 dates | 88.5m -> 75.1m, moving |
| CROX | 58,847,388 on all 7 dates | 74.1m -> 62.4m, moving |
| ALXN (acquired 2021) | absent on every date | 224.6m -> 219.2m, moving |

The weighted count starts moving only in 2022 and then converges on the
share-class count. So the vendor's weighted series appears to begin
around 2022; for earlier dates it returns its earliest value, and for
companies delisted before then it has none.

**Consequences of leaving it as it was** (all three now removed):

1. Every market cap before 2022 used a 2022 share count - future
   information in a past decision.
2. The error is one-sided: companies buy back shares, so old caps were
   understated (CROX by 21%, WSM 18%, A 7% in 2016), which distorts
   membership at both the $2bn and $50bn edges.
3. Companies that disappeared before 2022 had no count at all and were
   dropped. 9,327 stock-months were dropped this way; 2,842 of them had
   the dollar volume to make the top 400, concentrated in 2017-2019.
   That is survivorship bias in exactly the years the walk-forward
   evaluation needs most.

**Known limitation, accepted:** the share-class count covers one class of
shares, so for a multi-class company market cap is that class's, not the
whole company's. Such companies are detected (the two counts disagree on
a date where both are trustworthy, 2022 onwards) and logged to
`logs/multi_class_securities.csv`.

**Cost:** the stored share data held only the weighted count, so all
share counts are re-fetched (~190k requests, 5-6 hours) and every
monthly list rebuilt. Both counts are stored this time, so changing
field again would need no further downloading. Rebuilt months move the
previous list to `universe/superseded/<run>/`; snapshots are never
deleted.

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

## Amendment 5 (2026-09-22): foreign private issuers stay in - CONSIDERED AND DECLINED

**No change to Section 2.** Recorded because the change was proposed by
the project owner and deliberately not made; the next person to notice
these securities should find the reasoning rather than re-open it.

**What was proposed:** exclude the 71 universe members that are foreign
private issuers, on the same footing as Section 2 rule 3's exclusion of
ADRs, for the same reason - their earnings dates cannot be verified.

**Why it was declined.** Three things came up when the proposal was
costed:

**1. We could not identify them honestly the quick way.** The 71 are
known only because they have no 8-K item 2.02 filing anywhere in the ten
years - a fact assembled in 2026. Using it to build the 2017 universe is
look-ahead of exactly the kind that would make every downstream number
untrustworthy. It is fixable: foreign private issuers file 20-F or 40-F
annual reports, so "has filed a 20-F before this rebalance date" is
knowable on the date and provable from EDGAR. But it is a required extra
step, not a detail, and any future attempt at this exclusion must take
it.

**2. The stated reason cuts wider than the 71.** LEDGER-4 decision 7
found 53 ordinary US companies - Urban Outfitters, Energy Fuels, Echo
Global, DISH among them - whose earnings dates we equally cannot verify,
because they file results under item 8.01 instead of 2.02. A rule that
excludes foreign issuers for unverifiable earnings while keeping Urban
Outfitters is narrower than its own rationale.

**3. The protection already exists.** LEDGER-4's three-state earnings
flag marks both groups unknown rather than false. Section 11 drops
unknown days from validation, and the production report says "earnings
status unknown" rather than implying a clean day. Exclusion would buy
the same protection at the cost of a full rebuild - universe, minute
bars for the 100-200 replacement securities, features, events - and
would invalidate LEDGER-3's measured candidate yield, which was taken
on the universe as it stands.

**What would justify revisiting it.** Not data availability, which is
handled. The stronger argument is market structure: many foreign issuers
report **semi-annually rather than quarterly**, so their information
genuinely arrives on a different cadence, and Section 2's mid-cap
rationale is about genuine single-name order flow. If that turns out to
matter, the exclusion is worth the rebuild - and it should be argued on
those grounds, with the 20-F test above, not on verification difficulty.

**Scale, for whoever reads this next:** 71 securities, 1,569 of 46,400
universe member-months (3.4%), a median of 13 of 400 per month, trending
from 11 in 2017 to 17 in 2026.
