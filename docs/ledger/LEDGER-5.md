# LEDGER-5: Section 7 candidate filters

**Date:** 2026-09-22
**Class:** new code in the frozen area (`vpa.signal.candidates`),
implementing Sections 7.1, 7.2 and 7.3. No threshold is invented here:
every number is read straight off the frozen specification.

## The thresholds, as implemented

| | Family A (7.1) | Family B (7.2) |
|---|---|---|
| `vol_pct_slot_60` | >= 90 | >= 90 |
| `spread_atr` | <= 0.60 | - |
| `\|ret_atr\|` | <= 0.25 | - |
| `\|resid_ret_atr\|` | <= 0.25 | - |
| `upper_wick_frac` | - | >= 0.50 |
| `close_loc` | - | <= 0.35 |
| `failed_new_high` | - | true |
| `low_quality` | false | false |
| location | <= 1.5 ATR of 20-day high/low or prior-week high/low | <= 2.0 ATR of 20-day high, prior-week high, prior-month high or swing pivot high |

Budget (7.3): hard cap 60 a day, ranked by `vol_pct_slot_60` descending.

A test asserts each of these against the literal numbers, so changing
one silently is not possible - it is a Class 2 change or it is a failing
test.

## Real-data proof: ANF, 13 January 2025

The day Abercrombie gapped 15% (previous close 161.09, opened 146.96,
low 128.30, closed 135.75). `scripts/check_candidates.py` reproduces
this table:

```
slot  vol pct   spread    |ret|  |resid|   A near |     A
        >= 90   <= .60   <= .25   <= .25   <= 1.5 |
   0    100.0   11.282   11.107   10.780    0.238 |     -
   1    100.0    0.930    0.422    0.461    0.448 |     -
   2    100.0    0.855    0.275    0.226    0.311 |     -
   3    100.0    0.437    0.285    0.225    0.171 |     -
   4    100.0    0.449    0.301    0.276    0.028 |     -
   5    100.0    0.532    0.067    0.058    0.059 |   YES
   6    100.0    0.757    0.664    0.601    0.243 |     -
```

**Exactly one bar of seven is a candidate**, and the six rejections are
the interesting part - they show the filter discriminating rather than
waving everything through:

- **Slot 0**, the gap open, has a range of 11.3 ATR and a move of 11.1
  ATR. It is the single most dramatic bar of the day and it is *not* an
  effort/result anomaly - the effort produced an enormous result. A
  filter that fired here would be measuring drama, not the pattern.
- **Slots 3 and 4** are narrow enough but moved 0.285 and 0.301 ATR,
  just past the 0.25 limit.
- **Slot 6** is both too wide (0.757) and moved too far (0.664).
- **Slot 5** passes everything: 100th-percentile volume, a 0.53 ATR
  range, a net move of 0.067 ATR, 0.058 after the market's own move is
  removed, and sitting 0.059 ATR off its 20-day low. Heavy trading,
  no price movement, on support.

Family B fires on no bar that day: the largest upper wick is 0.28,
against a threshold of 0.50.

## Yield, measured against LEDGER-3

242 sessions sampled evenly across the ten-year store, universe members
only:

| | this implementation | LEDGER-3 |
|---|---|---|
| Family A per day | mean **24.8** | mean **24.8** |
| Family B per day | mean 4.0 | mean 4.4 |
| both families | mean 0.4 | not measured |
| candidates per day | mean 28.4, median 24 | median 31 |
| distinct stocks | mean 22.8, median 20 | ~20 (A), ~4.6 (B) |
| over the 60 cap | 12 of 242 (5%) | 2 of 58 (3%) |

**Family A's mean matches LEDGER-3 to the decimal.** Family B is lower,
which is expected and is the point of LEDGER-2 amendment 1: LEDGER-3
used the merged nearest-pivot column, which admitted support as though
it were resistance, and it flagged itself as "slightly generous" at the
time.

**Two of LEDGER-3's figures could not be reproduced.** Its Family A
median of 29 comes out as 21 here, and its "fired on every day sampled -
never zero" comes out as five sessions of 242 with no candidate. The
means agree exactly, so the filters agree; the difference is in how the
distribution was summarised. LEDGER-3's analysis code was not committed
("This is an analysis run, not committed detector code"), so this cannot
be settled. **Lesson recorded: an analysis whose numbers go into the
ledger should be committed with it.** `scripts/check_candidates.py`
exists so this entry's numbers do not have the same problem.

**Against Section 7.3's expectation**, which is "roughly 30-60 per day":
the measured median is **24** and the mean 28.4, a little **below** the
expected band rather than above it. The cap binds on 5% of sessions. No
threshold change is proposed - Section 18's reviewer question asked
whether the filters fire 5 times a day or 500, and the answer is neither
- but the band in 7.3 is optimistic by a few candidates a day and the
record should say so.

## Readings

### 1. A candidate is a bar, not a stock or a stock-day

Section 8.1 hands the AI "the candidate bar's complete feature vector"
and Section 8.2 returns a `signal_timestamp`. One stock can therefore
contribute several candidates in a day, and 7.3's budget counts bars.
Distinct stocks are reported alongside, because LEDGER-3 counted those
and the two figures are easy to confuse.

### 2. "Within X ATR of a level" is a distance, not a direction

Section 7.2 says "within 2.0 x daily ATR of a resistance reference". The
literal reading is implemented: a stock a little above its 20-day high
is near that level on the same footing as one a little below.

**This is the one place the wording is genuinely ambiguous, and it is
not a small difference.** Of 968 Family B candidates over 251 sampled
sessions, **276 (28.5%) sit above the level rather than below it**. A
directional reading - resistance only resists while price is under it -
would remove them.

There is a real argument for each. Section 7.2's plain-English gloss is
"right where it previously failed", which suggests approaching from
below. Against that, a stock that has just made a new high and failed to
hold it has been rejected somewhere, and `failed_new_high` already
captures that independently of any level.

**Raised with the project owner rather than settled here.**

### 3. The event condition is not part of being a candidate

Section 7 item 6 reads "No event flag on that date *(validation only; in
production, flagged and retained)*". So `candidates()` does not consult
the event table at all; `unflagged()` is a separate mask that validation
applies. Baking it in would make production quietly drop candidates on
exactly the days a trader most wants marked.

### 4. Unknown flags are excluded from validation, and both readings are reported

The project owner's decision. A day enters validation only when every
observable flag is known false (63.0% of security-days); the lenient
reading, which keeps unknowns, retains 69.6%. Both are computed so the
difference is measured rather than assumed.

### 5. A missing number never admits a bar

Every comparison is made so that a NaN fails it. A bar whose ATR could
not be computed, or which has no confirmed swing pivot yet, is not a
candidate - it is not "probably fine".

### 6. Ties in the cap are broken by ticker, then slot

Section 7.3 says "rank deterministically by `vol_pct_slot_60`
descending". That alone is not deterministic: the percentile is coarse
and whole groups of bars sit at exactly 100. Without a tiebreak, which
candidates survived a capped day would depend on the order rows came off
disk, and the set would change between runs on the same data.

## Deviations from the specification

### 1. No per-sector cap (Section 7.3)

Section 7.3 caps candidates at "8 per GICS sector so a single sector
move cannot consume the entire budget". **Not implemented: there is no
sector classification in this project yet** - Section 16's open item 2,
still undecided, and the reason LEDGER-2 decision 5 left
`sector_ret_day` and `sector_resid_ret_atr` empty.

The constant `PER_SECTOR_CAP = 8` exists, unused, with a comment saying
why, so it reads as a known gap rather than an oversight. The hard cap
of 60 and the deterministic ranking are implemented in full.

**What this costs:** on the 5% of sessions where the cap binds, a single
sector moving together could take more of the budget than Section 7.3
intends. The dropped count is recorded either way.

## A contamination case worth keeping

ANF's gap day is flagged **no known event** by Section 6, and it should
not be. ANF filed an 8-K that morning at 07:10 ET - before the open -
under **item 7.01**, the January sales update that caused the gap. We
collect item 2.02 only.

LEDGER-4's staleness guard cannot catch this: ANF files 2.02 regularly,
119 filings on record, so its record looks healthy. This is one missing
event inside a good record, not a long silence.

So the single clearest Family A candidate in this entry sits on a day
that validation would treat as clean and is not.

**The size of that hole, measured** (LEDGER-4, amendment 2): 88,431
announcements under items 7.01 or 8.01 with no results filing near them,
across 1,084 of 1,158 securities, 77% of them filed outside market
hours. Counting them would mark 9.1% of security-sessions rather than
today's 4.6%. What to do about it is open with the project owner; until
it is settled, Section 7's validation mask is known to admit some
announcement days as clean.

## How the rules were checked

Twelve deliberate breaks, each confirmed to fail the tests:

| Break | Caught |
|---|---|
| Volume threshold loosened from 90 to 80 | yes (2 tests) |
| Spread limit loosened from 0.60 to 1.00 | yes (2) |
| `ret_atr` compared signed, so a large fall passes | yes (1) |
| The market-residual condition dropped entirely | yes (2) |
| `low_quality` ignored | yes (1) |
| Location limit doubled from 1.5 to 3.0 | yes (2) |
| Family B given the merged pivot column, admitting support | yes (17) |
| A missing number admits a bar instead of failing it | yes (2) |
| The cap's ranking loses its tiebreak | yes (1) |
| The cap counts non-candidates | yes (1) |
| The strict validation mask admits unknown days | yes (1) |
| Daily cap raised from 60 to 100 | yes (4) |

## Still open

- The Section 7.2 location reading (reading 2), with the owner.
- The per-sector cap, blocked on Section 16 open item 2.
- How much company news the item-2.02 flag misses (LEDGER-4).
