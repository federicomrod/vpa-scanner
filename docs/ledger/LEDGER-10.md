# LEDGER-10: Pattern C implemented and put on live watch

**Date:** 2026-09-23
**Class:** new code in the frozen area (`family_c` in
`vpa.signal.candidates`), implementing the definition pre-registered in
`docs/pattern-c-preregistration.md` on the same day, **before** any test
against history.
**Still variant 2.** Implementing a pre-registered definition is not a
new variant; testing it will report under this number.

## Implemented exactly as written down

Every condition and threshold matches the pre-registration. Nothing was
adjusted, and the git history shows the definition committed first.

## The yield came out lower than I predicted

The pre-registration expected "likely 5 to 8 a day" after location and
direction filtering. Measured over 60 sampled sessions, universe
members only:

| | per day |
|---|---|
| Family A | 24.8 |
| Family B | 4.1 |
| **C-up** | **1.3** |
| **C-down** | **0.8** |
| **C total** | **2.1** |

**My estimate was wrong by a factor of three.** I assumed conditions 6
and 7 would roughly halve the 13 a day the stretch conditions produce;
they cut it by 84%. The direction condition is far more restrictive than
I allowed for - it requires both a close in the right third of the bar
**and** the nearest level to be the right kind, and those two agree less
often than I assumed.

**Nothing was changed in response.** Loosening a threshold to hit a
yield I guessed at is exactly the variant-hunting Section 11.4 counts
against us, and the guess has no standing against the definition. The
miss is recorded because a pre-registration that quietly absorbs its own
errors is worth nothing.

**It does not threaten Section 13.3.** At 1.3 and 0.8 a day across ~2,100
development sessions, each direction should clear the 250 date-clustered
occurrences comfortably. That will be measured, not assumed.

## Pattern C is kept out of `is_candidate`

`family_c_up` and `family_c_down` are recorded as their own columns and
**excluded** from `is_candidate`, which still means A or B only.

Sections 11 and 13 are measuring A and B. A third family silently
joining the candidate set would change what LEDGER-9's numbers mean,
would change which controls get drawn, and would make the two runs
incomparable. C joins that set when and if it is validated.

## The live watch, and what the email says

`vpa.data.scan` emails the day's C candidates. Every email:

- leads with **UNVALIDATED - Pattern C has not been tested**, before
  anything else, including when nothing fired;
- says that of the two patterns which *have* been tested, one was
  retired and the other did not pass;
- carries no recommendation, no entry, no size, no direction to act on -
  a test asserts the words "buy", "sell", "position size", "stop loss"
  and "target price" appear nowhere in it;
- says **SCAN UNAVAILABLE** with the error when anything fails, because
  a broken scan and a quiet day must never look alike (CLAUDE.md).

The subject line carries "unvalidated" too, since a notification may be
all that is read.

## A risk this creates, for the record

The project owner will be reading Pattern C candidates daily **while
producing the Section 12 blind labels**. Those labels are meant to be
independent of the system. Watching the system's picks every morning
teaches its taste, and a labeller who recognises the shape is no longer
judging independently.

Reasons this is probably modest: the labels are drawn from historical
sessions sampled across ten years, so they rarely overlap with what the
emails show; and the owner is an experienced VPA trader, so these shapes
are ones he already knew - Pattern C was written from that vocabulary,
not the other way round.

Reasons it is not nothing: the emails teach the system's **specific
thresholds**, which is a narrower thing than the vocabulary.

**Mitigation taken:** the email is text only. It names the ticker and
the numbers, and renders no chart, so the visual shape is not being
drilled morning after morning.

**Raised with the project owner rather than decided here.** The
alternative - hold the live watch until labelling finishes, four months
out - has its own cost, and it is his trade-off to make.
