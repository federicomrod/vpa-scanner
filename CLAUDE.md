# Project rules for vpa-scanner

This file is read by Claude (and should be read by any human contributor)
before making changes in this repository. It is not optional guidance -
the rules below are hard constraints, not preferences.

## What this project is

Every trading morning, this project scans US mid-cap stocks for Volume
Price Analysis (VPA) patterns and emails a short list of candidates to a
human trader. The trader reads the email and decides what, if anything,
to do about it.

**There is no automated trading in this project, ever.** Nothing here
places an order, connects to a broker, or takes any action in a
brokerage account. The only output is information for a person to read.

## The governing documents

Two documents are the authoritative specification for this project:

- `docs/concept-v2.md` - the frozen signal specification: what counts as
  a candidate and how it is detected.
- `docs/architecture-v2.md` - the infrastructure and change-control
  design: how the system is put together and how changes are reviewed.

If code and a governing document ever seem to disagree, the document is
right until the project owner says otherwise. Do not resolve the
disagreement by guessing - ask.

## Hard rules

1. **Never write code that places trades or connects to a broker.** Not
   even scaffolding, not even commented out, not even behind a flag that
   defaults to off. If a request seems to be heading in that direction,
   stop and say so explicitly instead of proceeding.

2. **`src/vpa/signal/` is the frozen specification area.** It implements
   Sections 2-13 of `docs/concept-v2.md`, and only that. Changing
   anything in this directory requires an experiment-ledger entry, and
   any pull request touching it must contain a line matching
   `Ledger: LEDGER-<number>` in its description - this is checked
   automatically by CI and the PR will be blocked without it. Never
   modify this directory as a side effect of doing something else (e.g.
   "while I was in there fixing the config loader, I also tweaked a
   threshold"). If a task seems to require touching this directory, stop
   and say so explicitly before making any change there.

3. **Never read, write, or reference `.env` files, the `holdout/`
   directory, or anything under `data/raw/`.** Tests and everyday
   development use the sample data in `fixtures/` instead. `.env` holds
   real secrets; `holdout/` and `data/raw/` hold real market data that
   must stay untouched by anything except the (future) production data
   loader.
   The one approved exception (project owner, Milestone 2): the production
   data loader's `src/vpa/data/secrets.py` reads named keys from
   `~/vpa-secrets/.env` when a download is run by the owner. Claude never
   opens that file itself, and never runs a live download.

4. **No network calls in tests, ever.** Anything that would talk to the
   internet in production (SMTP email, a push notification endpoint, a
   dead-man's-switch ping) must be swapped for an injectable fake in
   tests. A test suite that can run on an airplane with no wifi is a
   test suite that's actually testing the logic, not the network.

5. **All rolling windows in feature code must be strictly trailing and
   exclude the current bar.** A window that includes the current bar is
   look-ahead bias - the model would be "seeing the future" relative to
   the moment the signal is supposed to fire. This is the single most
   serious class of bug this project can have, because it can make a
   backtest look great while being worthless (or actively misleading) in
   real use. Get this wrong and every downstream number is untrustworthy.

6. **Keep pull requests small.** The project owner reviews on a phone.
   One logical change per pull request, with a plain-language
   description of what changed and why - assume the reader does not
   write code and hasn't seen this codebase before.

7. **Ask rather than assume.** If the specification (or a task
   description) is ambiguous, ask the project owner. Do not invent a
   reasonable-sounding interpretation and quietly proceed - a wrong
   guess here is expensive to unwind later.

8. **No hardcoded thresholds outside `src/vpa/signal/` and its
   configuration.** Magic numbers that affect what counts as a signal
   belong in the frozen spec area (and are subject to rule 2), not
   scattered through pipeline or reporting code where they'd be easy to
   change without anyone noticing.

## Working style expected in this repository

- **Plain language, always.** The project owner is not a developer.
  Explain what a change does and why in terms a non-developer can
  follow, in commit messages and PR descriptions alike. Avoid jargon
  where a plain description works just as well.
- **Fake data is fine for plumbing work, and must be labelled as fake.**
  Milestone 1 is explicitly about proving the pipeline works end to end,
  not about producing real signals. Any hardcoded or generated fake data
  must say clearly, in code and in output, that it is fake.
- **Fail loudly, never partially.** If any step of a scan fails, the
  output must be an unambiguous "SCAN UNAVAILABLE" message with the
  error - never a partial or silently-degraded report that looks like a
  normal result.
- **Tooling:** Python 3.12, dependencies managed with `uv` and a
  committed lockfile, `pytest` for tests, `ruff` for linting and
  formatting, `pydantic` for configuration and schema validation.
  Formatting and linting are enforced by pre-commit hooks and by CI -
  don't hand-format around them.
- **Docker is migration insurance, not a local workflow.** A `Dockerfile`
  is maintained and built in CI so the project could move to another
  machine if needed, but nobody is expected to use Docker for day-to-day
  development.
- **CI needs no secrets.** The GitHub Actions workflows in this repo run
  entirely without credentials. If a task seems to require a secret in
  CI, that's a sign of a design problem - stop and raise it with the
  project owner rather than adding the secret.
