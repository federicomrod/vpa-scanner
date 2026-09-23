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

---

## Amendment 1 (2026-09-22): macro flags, and two bugs the work exposed

**Class:** new code in the frozen area (`vpa.signal.events`), plus a
correction to a rule shipped in the original entry. Section 6's macro
flags - FOMC, CPI, non-farm payrolls - are now implemented.

### The date table is committed, not fetched

Ten years is 350-odd dates. Recalling them is out of the question and
fetching them at scan time would put the morning report at the mercy of
two government websites, so they are fetched once, checked, and
committed as `reference/macro-events.csv`. The build refuses to write
when a year comes back empty or a BLS release turns up at an unexpected
hour, both of which mean a page changed shape and was parsed wrongly.

Sources: federalreserve.gov for FOMC, bls.gov's yearly release schedule
for the other two.

### Deciding what counts as an FOMC decision - without my judgement

Not every dated Fed announcement is a policy decision. March 2020 alone
published statements on the 3rd, 15th, 19th, 23rd and 31st. Minutes do
not settle it: they cover the eight scheduled meetings, so they miss the
emergency cut of 3 March and lag the most recent meeting by three weeks.

**The Fed labels them itself.** A policy statement from the Committee is
titled "Federal Reserve issues FOMC statement", and that exact wording
has been used for every one from 2016 to 2026, scheduled and emergency
alike, while framework updates, facility announcements and regulatory
rules are titled differently. Each statement page is fetched and kept on
its own title. I originally intended to hand the ambiguous dates to the
project owner to classify; the Fed's own naming made that unnecessary,
which is better - it is reproducible.

Result: 88 FOMC days over eleven years. 2020 has ten, correctly - the 3
and 23 March emergency statements are in; the 19 and 31 March facility
announcements and the 27 August framework update are out.

**Cross-check kept:** every meeting that published minutes must have a
statement behind it, or the build stops. It passes.

### Bug 1: news from before the store landed on its first session

`searchsorted` returns position 0 for any date before the range, so
**every release older than the store was pinned to the store's first
session**. Companies file 8-Ks back to 2004. Measured in the table
shipped with the original entry: **739 of 817 securities carried an
earnings flag on 2016-10-03**, and 740 on the 4th, against 3 to 8 on an
ordinary day.

Fixed: news with no session to react to comes back as unknown at both
ends of the range, not just the late end. After the fix, 4 and 8.

It was caught because the macro flags made it visible - the first
session came back flagged for FOMC, CPI *and* payrolls at once, which is
impossible. The same bug had been sitting in the earnings flag,
unnoticed, through a full review and a green CI run.

### Bug 2: the open was the boundary, and it should have been the close

The original rule read: published before 09:30, this session trades on
it; at or after 09:30, the next one. That is right for a release after
the close and **wrong for one during the session** - the market is open
and reacts within the minute.

Measured over the store's 75,995 results filings:

| when the filing was accepted | share |
|---|---|
| before the open | 43.5% |
| **during the session** | **13.9%** |
| at or after the close | 42.6% |

So roughly one earnings day in seven was being attributed to the wrong
session. The rule is now: **published before the close, that session;
at or after the close, the next one.** The open does not come into it.

Half days close at 13:00, so the boundary moves with them; the close
times come from `vpa.signal.bars`, so only one place says when trading
stops.

### A macro flag is never unknown

Unlike a company's earnings, the release calendar is complete - every
one of these is scheduled and published in advance - so a quiet day is a
known-quiet day, and `any_event` can rely on it.

### Deliberately not included

**Forward-looking dates.** The table ends where the published schedules
end. The live morning scan will need the next FOMC and CPI dates ahead
of time; that is a production concern, recorded with the same gap for
earnings dates.

### How this was checked

67 tests, no network. The two bugs above each have a regression test
naming the measured numbers. The rebuilt table was checked against the
store: 2,434,142 rows, the first sessions back to normal, OHI's 16:18 ET
filing still flagging 5, 6 and 7 February 2025, and 3 March 2020 and 16
March 2020 both flagged FOMC for every security.

---

## Amendment 2 (2026-09-22): how much the item-2.02 flag misses, measured

**Class:** measurement, plus the code to take it. No flag or threshold
changes; the decision it informs is still open.

### Why this was taken

Decision 7 marks a security's earnings flag unknown when its item-2.02
record goes **quiet**. That catches a company which stopped using the
code. It does not catch a company which uses 2.02 for quarterly results
and something else for an interim announcement.

**Abercrombie, 13 January 2025** is the case that forced this. The stock
gapped 15% - previous close 161.09, open 146.96, low 128.30 - on a
January sales update filed that morning at 07:10 ET, before the bell,
under **item 7.01**. ANF's 2.02 record is healthy, 119 filings, so the
staleness guard sees nothing wrong. Section 6 reports **no known event**
for the day, and Section 7 would admit it to validation as clean.

**A correction to this entry's decision 1**, which claimed "ANF's
January sales update is one" of the item-2.02 filings. It is not. The
only January 2.02 on ANF's record in the whole ten years is 2014-01-10.
The claim was wrong when written.

### What was measured

Every 8-K for all 1,158 securities was downloaded - 268,530 filings,
reusing the CIKs already resolved, so no vendor key and no vendor
requests. Then: 8-Ks under item **7.01** (Reg FD) or **8.01** (Other
Events) with no results filing within a day either side, since companies
routinely file those alongside their results.

| | |
|---|---|
| 8-Ks on record | 268,530 |
| under 7.01 or 8.01, no results filing near | **88,431** |
| securities affected | 1,084 of 1,158 |
| filed before the open | 26,750 (30%) |
| filed after the close | 41,328 (47%) |

**77% were filed outside market hours** - the shape of a news release
rather than routine housekeeping.

### What widening the flag would cost

| flag | security-sessions marked (D-1/D0/D+1) |
|---|---|
| item 2.02 only, as now | 111,902 (4.6%) |
| voluntary items only | 111,680 (4.6%) |
| both | **221,868 (9.1%)** |

So these announcements would flag **as many days again** as the results
filings do. Widening the earnings flag to include them doubles its
footprint.

### Not decided here

Three options, none free:

1. **Leave the flag as it is.** ANF-type days stay in validation,
   unflagged. The size of that is now known rather than guessed.
2. **Widen `earnings` to 7.01 and 8.01.** Doubles the excluded days and
   calls a buyback announcement "earnings", which is not true.
3. **Add a separate `company_announcement` flag.** Keeps the two
   distinct, lets Section 7's mask include it or not, and makes the
   choice measurable either way. This is the recommendation on the
   record; the project owner decides.

---

## Amendment 3 (2026-09-23): the missed announcements are mostly noise

**Class:** measurement. Answers the question amendment 2 left open, and
reverses the direction it was leaning.

Amendment 2 found 88,431 announcements the earnings flag says nothing
about, and noted that counting them would take the flag from 4.6% of
company-days to 9.1%. The open question was whether those days carry
real news. `scripts/check_announcements.py` measures it, against each
stock's own strictly-trailing baseline.

### How these days actually behaved

Over 2,416,062 security-sessions:

| group | days | move in ATR (median / p90) | volume vs median (median / p90) |
|---|---|---|---|
| item 2.02, known earnings | 37,220 | **1.23** / 3.99 | **2.61** / 5.96 |
| 7.01 or 8.01, no results near | 38,585 | **0.47** / 1.41 | **1.12** / 2.48 |
| every other day | 2,340,272 | **0.40** / 1.08 | **0.98** / 1.73 |

A voluntary-item day moves 0.47 ATR against an ordinary day's 0.40, on
14% more volume. An earnings day moves 1.23 ATR on 161% more volume.
**These days sit far closer to an ordinary day than to an earnings
day.**

Share producing a move of 2 ATR or more - the size earnings days
routinely make:

| group | share | days |
|---|---|---|
| known earnings | **31.9%** | 11,867 |
| voluntary item | **4.6%** | 1,791 |
| every other day | **1.3%** | 29,517 |

### No cheap rule separates the signal from the noise

Filing characteristics were tested as a way to isolate the real
announcements without looking at market reaction, which would be
circular. None of them separate anything:

| subset | days | median move | median volume |
|---|---|---|---|
| all voluntary-item | 38,585 | 0.47 | 1.12 |
| with item 9.01 (press release attached) | 31,071 | 0.47 | 1.13 |
| with 9.01 **and** filed outside market hours | 26,948 | 0.47 | 1.14 |
| without 9.01 | 7,650 | 0.46 | 1.08 |

78% carry an attached press release and 70% are filed outside market
hours, and neither tells us anything - both subsets behave like the
whole.

### What this means for the decision

Folding these into the earnings flag would **double the days excluded
from validation** to capture 1,791 event-sized days out of 38,585. The
other 95.4% are, on the evidence, ordinary days.

For scale: **29,517 ordinary days with no announcement at all also moved
2+ ATR**. Unexplained large moves are already common and are mostly not
caused by missing 7.01/8.01 filings - adding the flag would account for
about 5.7% of the large non-earnings moves in the store.

**Recommendation, for the project owner:** do not fold these into
`earnings`. Record them as a separate `company_announcement` flag that
is **marked in production and left out of the validation mask**. The
trader gets "an 8-K was filed at 07:10" next to an unusual bar, which is
genuinely useful, and validation does not lose 4.6% of its days to
buyback notices.

**The residual, stated plainly:** ANF's 13 January 2025 gap stays in the
1,791. The clearest Family A candidate in LEDGER-5 sits on a day
validation treats as clean, and under this recommendation it still
would. That is a known, measured cost, not an oversight.

### Decided and built (project owner, 2026-09-23)

The recommendation was accepted: a separate `company_announcement` flag,
**recorded and shown, never grounds for excluding a day from
validation**. `OBSERVABLE_FLAGS` therefore excludes it, alongside the
two flags that have no source - the same exclusion for opposite reasons,
and both named in the code so neither reads as an oversight.

**The window is the reacting session only**, not D-1/D0/D+1 as earnings
uses. D-1 exists for earnings because a scheduled report is anticipated
and positioned for; nobody positions ahead of an unscheduled 8-K.

Measured on the rebuilt table, 98,105 sampled security-days: the flag is
true on **1.6%** of them, and **81% of those days still read `any_event`
false**, which is the whole point - they stay in validation.

ANF's 13 January 2025 now reads `company_announcement: true` with
`any_event: false`. The trader would see "an 8-K was filed that
morning"; validation still treats the day as usable. The owner accepted
that explicitly: the aggregate evidence decides this, not one familiar
example.

### A caution on the obvious shortcut

Selecting these announcements *by how much the stock moved* and flagging
only those would be circular: Section 11 excludes flagged days from the
validation of a volume-and-price pattern, so choosing which days to flag
by their volume and price would quietly remove the very days the pattern
is being tested on. `EVENT_SIZED_MOVE` in the script exists to describe
the tail, never to define a flag.
