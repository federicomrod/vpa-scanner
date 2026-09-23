# Putting the holdout out of reach

Concept v2 Section 11.4:

> The holdout and evaluation data live **outside the repository**, in a
> directory neither the cloud coding agent nor Remote Control can read.

Today that separation is **by date in code, not by filesystem**. The
whole store sits in `~/vpa-data`, which the assistant reads freely, and
`scripts/check_kill_criterion.py` simply declines to look at anything on
or after `HOLDOUT_FROM`. That is a rule being followed, not a rule being
enforced, and the difference matters for a number that gets looked at
exactly once.

These are the steps to close it properly. **Run them yourself** - the
assistant should not be the one moving data it is not allowed to see.

## What has to move

The boundary is **2025-09-19**, the last 12 months of the store.

Moving only the derived data would not be enough: the raw minute bars
can rebuild it. Everything below has to go.

| what | where |
|---|---|
| raw minute bars | `~/vpa-data/raw/minute/date=2025-09-19` onwards |
| hourly and daily bars | `~/vpa-data/derived/hourly/`, `derived/daily/` |
| features | `~/vpa-data/derived/features/hourly/`, `features/daily/` |
| event flags | `~/vpa-data/derived/events/` |
| outcomes | `~/vpa-data/derived/outcomes/` |
| universe snapshots | `~/vpa-data/universe/2025-10-01.parquet` onwards |

`scripts/plan_holdout.py` lists exactly which directories match and how
much they come to, and **moves nothing**.

## The mechanism: an encrypted image, normally unmounted

A second user account would work, but an encrypted disk image is simpler
and does not need one. While it is unmounted the data is unreadable to
every process on the machine, including this assistant, without needing
anyone to remember a rule.

    # once, to create it
    hdiutil create -size 30g -encryption AES-256 -type SPARSEBUNDLE \
        -fs APFS -volname VPAHoldout ~/vpa-holdout.sparsebundle

    # mount it (asks for the password)
    hdiutil attach ~/vpa-holdout.sparsebundle

    # move the holdout across - see plan_holdout.py for the exact list
    uv run python scripts/plan_holdout.py --move

    # and put it away
    hdiutil detach /Volumes/VPAHoldout

Keep the password somewhere the assistant has never seen, which means
not in `~/vpa-secrets/.env` and not in this repository.

## Afterwards

- `~/vpa-data` ends at **2025-09-18**. Every pipeline will simply find
  no files past that date, which is the intended behaviour.
- The development numbers already produced are unaffected: they were
  computed on data before the boundary.
- **The one evaluation** is run with the image mounted, by you, once.
  Section 11.4 means once - not once per attempt.

## What still leaks, and why it is accepted

The corporate-action tables (`raw/splits`, `raw/dividends`) are
partitioned by the date they were **fetched**, not by session, and they
already contain rows dated into the future - a dividend with an ex-date
in 2026, for instance. Splitting them by session is not possible without
rewriting them.

This is accepted because those tables carry no price or outcome
information. Knowing that a stock goes ex-dividend on a future date
reveals nothing about what its price did. If that judgement ever looks
wrong, the tables can be rebuilt with a session partition.
