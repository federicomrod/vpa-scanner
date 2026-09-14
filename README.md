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
- The report can be emailed to you, pushed to your phone as a short
  summary, and the run pings a monitoring service so you'd find out if
  the scan silently stopped running. See "Setting up delivery" below
  for the one-time setup this needs before it can actually send you
  anything for real.
- There's now a supervisor script (`scripts/run_scan.sh`) meant to be
  the one thing a scheduled job calls each morning: it makes sure two
  scans never overlap, skips non-trading days, only runs from a
  released version of the code, retries if something transient goes
  wrong, and always tells you the outcome one way or another.

The sections below describe how the project is organised, what you
need to set up for delivery to work, and how to actually run it.

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
| `src/vpa/trading_calendar.py` | Works out whether today is a trading day, so the scan can skip weekends and market holidays. |
| `src/vpa/pipeline.py` | Ties everything together: run the scan, build the report, send it out. |
| `src/vpa/cli.py` | The two commands `scripts/run_scan.sh` actually calls - see that script for why they're split in two. |
| `tests/` | Automated checks that the code does what it's supposed to, using fake data - no real market data or internet connection involved. |
| `fixtures/` | Small fake sample data files used by the tests. |
| `scripts/run_scan.sh` | The supervisor script a scheduled job (cron, launchd, ...) actually calls each morning. |

## Setting up delivery (email, push notifications, monitoring)

Three one-time things need to be set up before the project can actually
send you anything. None of them go into this repository, get typed into
a chat with Claude, or get shared with anyone else - they're private
values that only ever live in one small file on the computer that runs
the scan (see "Where the secrets actually go" further down).

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

## Where the secrets actually go

`scripts/run_scan.sh` (the supervisor script) reads a private file
whose *path* you tell it about via an environment variable,
`VPA_ENV_FILE` - the file itself is never part of this repository, and
its path can be anywhere you like (for example
`~/.config/vpa-scanner.env`, kept readable only by you). It looks like
this:

```
VPA_EMAIL__FROM_ADDR=you@gmail.com
VPA_EMAIL__TO_ADDR=you@gmail.com
VPA_EMAIL__SMTP_PASSWORD=<the Gmail App Password from step 1 above>
VPA_PUSH__NTFY_TOPIC=<the ntfy.sh topic from step 2 above>
VPA_DEADMAN__BASE_URL=<the healthchecks.io ping URL from step 3 above>
```

## How to run it

This project uses a tool called `uv` to manage its Python dependencies,
so nothing needs to be installed by hand, beyond two small system tools
`scripts/run_scan.sh` relies on:

- **`git`** and **`uv`** (you already have these, since you're reading
  this from a checkout).
- **`flock`** and **`timeout`** - standard on Linux, but not built into
  macOS. On a Mac: `brew install flock coreutils`.

Everyday commands:

- Install everything the project needs: `uv sync`
- Run the automated tests: `uv run pytest`
- Check the code style: `uv run ruff check .`

To actually run a full scan (build the report and send it), set
`VPA_ENV_FILE` to your private secrets file from above, and run the
supervisor script from a tagged commit (see "Why does it need a git
tag?" below):

```
git tag v0.1.0   # only needed once, or whenever you want a new release point
VPA_ENV_FILE=~/.config/vpa-scanner.env ./scripts/run_scan.sh
```

That builds the (still fake-data) report, writes it to `reports/`
(a folder that's never committed to git - see `.gitignore`), emails it
to you, sends a one-line push notification, and pings your monitoring
check. If today isn't a trading day, it does nothing and exits quietly.
If anything goes wrong, it retries automatically, and if it still can't
recover, it emails and pushes a clear "SCAN UNAVAILABLE" instead of
staying silent.

### Why does it need a git tag?

The supervisor script refuses to run unless the code it's running is
checked out at an exact git tag (e.g. `v0.1.0`), rather than whatever
the latest commit happens to be. This is a deliberate safety check: it
means the version that actually runs and emails you every morning is
always one you (or a reviewer) deliberately marked as ready, never
whatever's mid-edit. To update what the live version runs, merge your
changes to `main` as usual, then create a new tag on the commit you
want to promote (`git tag v0.1.1 && git push origin v0.1.1`).

## A note on safety

A lot of this project's design exists specifically to prevent mistakes:
things like never letting tests touch the internet or real market data,
requiring an explicit sign-off before the core pattern-detection logic
can change, and always sending a clear "scan failed" message rather than
a silently incomplete report. If any of that ever seems to be getting in
the way rather than helping, that's worth a conversation, not a
workaround.
