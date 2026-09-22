# LEDGER-4: Section 6 event flags

**Date:** 2026-09-22
**Class:** new code in the frozen area (`src/vpa/signal/events.py`),
implementing Section 6. No existing threshold or rule changes.
**Supersedes nothing.**

## What was built

Every ticker-day now carries the flags Section 6 names, stored at
`derived/events/date=YYYY-MM-DD/events.parquet`:

| Flag | Source |
|---|---|
| earnings (D-1, D0, D+1) | SEC EDGAR 8-K item 2.02 (LEDGER-4 decision 1) |
| ex-dividend | vendor corporate actions, `ex_dividend_date` |
| split | vendor corporate actions, `execution_date` |
| index addition/deletion | **none exists** - unknown always |
| trading halt | **none exists** - unknown always |
| option expiry, triple witching | derived from the session list |
| month-end, quarter-end | derived from the session list |
| half day | the bars' own `is_half_day` |
| day after a market holiday | derived from the session list |

Macro flags (FOMC, CPI, non-farm payrolls) are **not** in this change.
They need a sourced, committed date table covering ten years, which is a
separate piece of work with a separate failure mode, and bundling it here
would have made this PR two changes.

## The decisions

### 1. "Earnings" means "results announcement"

Item 2.02 is *Results of Operations and Financial Condition*. It covers
quarterly earnings, but also mid-quarter updates and pre-announcements -
ANF's January sales update is one. This is treated as a feature, not
trimmed: Section 6 exists to mark days when something known was going on,
and a company publishing financial results qualifies. **The flag means
"results announcement", slightly broader than "quarterly earnings".**

### 2. The acceptance timestamp decides which session reacted

A release accepted before 09:30 ET is traded that same session; one
accepted at or after the bell is first traded the next one. Most
companies report after the close, so the common case is D0 = the
following session. A date-only earnings feed cannot make this
distinction, and getting it wrong shifts the whole D-1/D0/D+1 window by
a day.

Checked against real filings: ANF at 07:38 ET (same session), OHI at
16:17 ET (next session).

### 3. The window is measured in sessions, not calendar days

D-1 for a Monday announcement is the previous Friday; D-1 for the Monday
after Good Friday is the Thursday before it.

### 4. Three states, and where each unknown comes from

A flag is true, false, or **unknown**. Section 11 excludes flagged days
from validation, so a flag reading false where the truth is merely
unavailable would quietly leave contaminated days in the clean set, and
nothing about the output would look wrong.

Unknown arises two ways, and they are kept apart on purpose:

**Per security - earnings for foreign private issuers.** 71 of the 1,159
securities that have passed through the universe file 6-K and 20-F/40-F
rather than 8-K, so no item 2.02 filing exists for them at any price.
They are legitimately in the universe (all typed `CS` on NYSE, Nasdaq or
NYSE American; Section 2 rule 3 excludes ADRs, and these are directly
listed ordinary shares). They account for **1,569 of 46,400 universe
member-months, 3.4%** - a median of 13 of 400 per month, trending from
11 in 2017 to 17 in 2026. Their earnings flag is unknown on every day,
`any_event` carries that through, and validation can drop them.

Measured over the store as built: **101 of the 1,186 securities with
bars hold no results filings at all** - 73 universe members with a CIK
but no 2.02 filing (the foreign private issuers and a few others), one
with no CIK (Thomson Reuters, Canadian), and 27 that never entered a
universe snapshot at all, which includes SPY. Together that is **183,915
of 2,434,142 security-sessions, 7.56%**.

**For everyone, always - halts and index changes.** Massive carries halts
only on a real-time feed whose documentation says history is "not
applicable", and has no index-membership endpoint at all. Both are
recorded as columns that are unknown on every row.

### 5. `any_event` excludes the two always-unknown flags

This is the one place the three-state discipline is deliberately
relaxed, and it needs stating plainly.

If `any_event` folded in halt and index_change, it would be unknown on
every row ever written, and validation would have nothing left to
exclude against - the flag would be unusable and Section 11 could not
run. So `any_event` is computed over the flags that have a source:

> true if any is true; **unknown** if any is unknown; false only when
> every one of them is known to be false.

**The consequence, stated rather than hidden: "no known event" means "no
event among those we can observe", not "no event".** A day flagged clean
may still have been a halt day or an index-rebalance day. The two
columns remain in the table so that if a source is ever found, the gap
can be filled without changing the rest.

### 6. The frozen area still looks nothing up

`vpa.signal.events` takes the session list, which of them were half days,
and the dated company events, and returns flags. It imports no calendar
and reads no store - the same boundary `vpa.signal.bars` keeps, where the
caller supplies the session open and close. Every calendar rule is
therefore derived from the ordered session list itself: the monthly
expiry is the last session on or before the third Friday (which handles
Good Friday without a holiday table), a month-end is a session whose
successor is in a different month, and a day-after-holiday is a session
with a weekday gap behind it.

A side effect worth knowing: the **first and last sessions of whatever
range you pass carry an unknown** where the answer depends on a neighbour
that was not supplied. The builder passes the whole store, so this
affects only its two outermost sessions.

### 7. Item 2.02 is not used consistently, and a stale record is unknown

**This was found by running the coverage report, and it changed the
design.** Marking a security unknown only when we hold *no* filings for
it misses the worse case: a security whose filings *stop* while it keeps
trading. Those days read a confident false.

Item 2.02 is the SEC's designated code for results, but companies are not
obliged to use it, and some use item 8.01 ("Other Events") or 7.01 ("Reg
FD Disclosure") instead. Verified directly against EDGAR:

| | last 2.02 filing | 8-K items used afterwards |
|---|---|---|
| Urban Outfitters (URBN) | 2016-11-23 | 8.01 x 66, 9.01 x 68 |
| Energy Fuels (UUUU) | 2016-03-08 | 8.01 x 70, 9.01 x 86 |
| Echo Global (ECHO) | 2017-02-24 | 8.01 x 25, 9.01 x 45 |
| DISH Network | 2023-01-17 | 8.01 x 15, 9.01 x 24 |
| IHS Markit (INFO) | 2016-06-28 | 7.01 x 3 |

These are not companies that stopped reporting; they are companies that
stopped using item 2.02 to do it. URBN alone has 2,340 trading days that
would otherwise have read "no earnings" with confidence.

**The rule:** a session's earnings flag is trusted only when a results
filing lies within `STALE_AFTER_SESSIONS` (126, two reporting quarters)
**either side** of it. Looking both ways handles the three shapes
identically - before a company's first filing, after its last, and a hole
in the middle - and a genuine quarterly reporter, filing about every 63
sessions, is never touched by it.

**What it costs**, measured over all 2,434,142 traded security-sessions:

| `stale_after` | extra sessions unknown | share | securities |
|---|---|---|---|
| 63 (one quarter) | 46,013 | 1.89% | 143 |
| 95 | 39,971 | 1.64% | 65 |
| **126 (chosen)** | **36,052** | **1.48%** | **53** |
| 189 | 32,318 | 1.33% | 41 |
| 252 (a year) | 29,254 | 1.20% | 36 |

The curve is flat - a fourfold change in the threshold moves the cost by
0.7 percentage points - so the choice is not finely balanced, and 126 was
taken as the round middle. **Total earnings-unknown after this change:
9.04% of security-sessions** (7.56% with no filings at all, 1.48% stale).

### Alternatives considered and rejected

- **Widen the item set to 8.01 and 7.01.** These codes carry dividend
  declarations, buybacks, conference appearances and much else; URBN
  files 66 of them. It would trade a false negative for a great many
  false positives, and Section 6 flags are used to *exclude* days.
- **Use EDGAR's XBRL `companyfacts` API**, which dates each reported
  period independently of item codes. This would genuinely fix the gap
  rather than mark it unknown. It is a separate download of comparable
  size to the one already run, and is the right next step if 9% unknown
  turns out to bind. Recorded, not built.

## How the rules were checked

Thirteen deliberate breaks, each confirmed to fail the tests:

| Break | Caught |
|---|---|
| Acceptance time ignored; every release reacts the day it was filed | yes (5 tests) |
| Window collapsed to D0 only | yes (6) |
| Window loses D-1 | yes (5) |
| Expiry rolls forward over Good Friday instead of back | yes (5) |
| "Third Friday" computed as the second | yes (4) |
| Triple witching on every monthly expiry | yes (1) |
| `any_event` treats unknown as false | yes (2) |
| `any_event` folds in halt and index_change | yes (2) |
| A weekend counted as a market holiday | yes (3) |
| A security with no filings flagged false instead of unknown | yes (1) |
| An ex-dividend applied to every security that day | yes (3) |
| A stale filing record still trusted to mean "no earnings" | yes (3) |
| Staleness checked backwards only, not both ways | yes (6) |

## Still open

- **Macro flags** - FOMC, CPI, non-farm payrolls. Separate PR, needs a
  sourced date table.
- **The XBRL route to earnings dates** (see above), if 9% unknown binds.
- **Forward-looking earnings dates.** EDGAR is a record of what was
  filed, so it cannot say a company reports *tomorrow*. The live morning
  scan will need a forward calendar; historical validation does not.
