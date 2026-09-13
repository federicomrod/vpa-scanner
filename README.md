# vpa-scanner

## What this is

Every trading morning, this project is meant to scan US mid-cap stocks
for a specific kind of chart pattern (called Volume Price Analysis, or
VPA) and email a short list of interesting candidates to a human trader.
The trader reads the email and decides what to do, if anything.

**This project never trades automatically.** It only ever produces
information for a person to read by email. There is no connection to a
brokerage account anywhere in this code, and there never will be.

## Where this stands right now

This is **Milestone 1**: getting the plumbing and the safety rules in
place before any real scanning logic is written. Concretely, that means:

- The project's folder layout, dependencies, and safety rules
  (`CLAUDE.md`) exist.
- Settings now load from `config.yaml`, and there's a "hello world"
  version of the pipeline that generates three hardcoded, clearly-fake
  candidate rows, stamps them with tracking information (which code
  version produced them, which configuration was used), and writes an
  actual Markdown report plus a matching JSON file to a `reports/`
  folder.
- The actual "look at stock data and find patterns" logic does **not**
  exist yet - it comes in a later milestone, once the specification
  documents in `docs/` are finalised.
- The report can now be emailed to you, pushed to your phone as a
  short summary, and the run pings a monitoring service so you'd find
  out if the scan silently stopped running. See "Setting up delivery"
  below - there are a few one-time things you'll need to set up
  yourself before any of this can actually send anything (nothing has
  actually been sent yet - that requires the supervisor script from
  the next pull request, plus the setup below).

The sections below describe how the project is organised, what you
need to set up for delivery to work, and how to run things by hand in
the meantime.

## How it's organised

| Folder / file | What it's for |
|---|---|
| `CLAUDE.md` | The project's rules - safety constraints, what can and can't be changed, and how work should be done. Worth a skim even if you don't write code. |
| `docs/` | The design documents that describe exactly what a "candidate" is and how the whole system is put together. These are the source of truth. |
| `src/vpa/signal/` | The **frozen specification area** - the actual pattern-detection logic, once it exists. Changes here are deliberately made hard to slip in by accident (see `CLAUDE.md`). |
| `src/vpa/data/` | Code that will load stock market data. |
| `src/vpa/reporting/` | Turns results into the Markdown report and JSON file that get emailed. |
| `src/vpa/delivery/` | Sends the report by email, sends a short push notification, and pings the monitoring service. |
| `src/vpa/reviewer/` | Anything that supports the human trader's own review process. |
| `src/vpa/config.py` | Loads settings from `config.yaml` and the environment. |
| `src/vpa/pipeline.py` | Ties everything together: run the scan, build the report, send it out. |
| `tests/` | Automated checks that the code does what it's supposed to, using fake data - no real market data or internet connection involved. |
| `fixtures/` | Small fake sample data files used by the tests. |
| `scripts/run_scan.sh` | The script a scheduled job actually calls each morning (coming in a later pull request). |

## Setting up delivery (email, push notifications, monitoring)

Three one-time things need to be set up before the project can actually
send you anything. None of them go into this repository or get shared
with anyone else - they're private values that only live on whatever
computer eventually runs the scan (the "supervisor script" coming in
the next pull request will explain exactly where to put them). For now,
it's enough to create these and keep them somewhere safe, like a
password manager.

### 1. A Gmail "App Password" (for sending the email)

Gmail won't let a program send email using your normal password - you
need a separate 16-character "App Password" instead, which you can
switch off at any time without affecting your real password.

1. Make sure **2-Step Verification** is turned on for your Google
   account (Google requires this before it will let you create an App
   Password). If you're not sure, go to
   [myaccount.google.com/security](https://myaccount.google.com/security)
   and check.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create a new app password - call it something like `vpa-scanner` so
   you remember what it's for.
4. Google will show you a 16-character code. Copy it somewhere safe -
   this is what becomes `VPA_EMAIL__SMTP_PASSWORD`.

The report will be sent from and to your own Gmail address.

### 2. An ntfy.sh topic (for the phone notification)

[ntfy.sh](https://ntfy.sh) needs no sign-up. You just pick a "topic"
name - think of it like a private channel name - and anything sent to
that topic shows up as a notification on your phone.

1. Install the **ntfy** app from the App Store or Google Play (or use
   it in a browser at ntfy.sh).
2. Pick a topic name that's hard for a stranger to guess - something
   like `federico-vpa-a8f3k2`, not `vpa-scanner`. Anyone who knows your
   topic name can send you notifications on it, so treat it a bit like
   a password.
3. In the app, subscribe to that topic name.
4. That topic name is what becomes `VPA_PUSH__NTFY_TOPIC`.

### 3. A healthchecks.io check (the "is it still running?" monitor)

This is a safety net: if the scan ever silently stops running
altogether (the computer is off, a scheduled job stops firing, etc.),
you'd otherwise never know - there'd be no email to notice was missing.
A monitoring service solves this by expecting a "ping" every morning
and alerting you if one doesn't show up.

1. Create a free account at [healthchecks.io](https://healthchecks.io).
2. Create a new check (name it `vpa-scanner`) and set its schedule to
   about how often the scan should run (e.g. once a day, on weekdays).
3. Copy the "ping URL" it gives you.
4. That URL is what becomes `VPA_DEADMAN__BASE_URL`.

## How to run it (for later, once there's something to run)

This project uses a tool called `uv` to manage its Python dependencies,
so nothing needs to be installed by hand.

- Install everything the project needs: `uv sync`
- Run the automated tests: `uv run pytest`
- Check the code style: `uv run ruff check .`

There's no single command yet that runs a scan *and actually sends it*
- that's what the supervisor script (next pull request) will wire up,
using the values from "Setting up delivery" above. In the meantime,
you can see the pipeline produce a real (fake-data) report on its own:

```
uv run python -c "
from pathlib import Path
from vpa.config import load_config
from vpa.pipeline import run_and_write_report
print(run_and_write_report(load_config(Path('config.yaml'))))
"
```

That writes a Markdown report and a matching JSON file into `reports/`
(a folder that's never committed to git - see `.gitignore`), using
today's date as the filename. This section will be updated with the
real end-to-end command once the supervisor script exists.

## A note on safety

A lot of this project's design exists specifically to prevent mistakes:
things like never letting tests touch the internet or real market data,
requiring an explicit sign-off before the core pattern-detection logic
can change, and always sending a clear "scan failed" message rather than
a silently incomplete report. If any of that ever seems to be getting in
the way rather than helping, that's worth a conversation, not a
workaround.
