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
- Still to come in this milestone: actually sending that report out by
  email and push notification - see the folder guide below for where
  that will live.

Because delivery isn't built yet, there's no single command that does
the whole job end to end. The sections below describe how the project
is organised and how it will be run once the next pull request fills
that in.

## How it's organised

| Folder / file | What it's for |
|---|---|
| `CLAUDE.md` | The project's rules - safety constraints, what can and can't be changed, and how work should be done. Worth a skim even if you don't write code. |
| `docs/` | The design documents that describe exactly what a "candidate" is and how the whole system is put together. These are the source of truth. |
| `src/vpa/signal/` | The **frozen specification area** - the actual pattern-detection logic, once it exists. Changes here are deliberately made hard to slip in by accident (see `CLAUDE.md`). |
| `src/vpa/data/` | Code that will load stock market data. |
| `src/vpa/reporting/` | Turns results into the Markdown report and JSON file that (eventually) get emailed. |
| `src/vpa/reviewer/` | Anything that supports the human trader's own review process. |
| `src/vpa/config.py` | Loads settings from `config.yaml` and the environment. |
| `src/vpa/pipeline.py` | Ties everything together: run the scan, build the report, send it out. |
| `tests/` | Automated checks that the code does what it's supposed to, using fake data - no real market data or internet connection involved. |
| `fixtures/` | Small fake sample data files used by the tests. |
| `scripts/run_scan.sh` | The script a scheduled job actually calls each morning (coming in a later pull request). |

## How to run it (for later, once there's something to run)

This project uses a tool called `uv` to manage its Python dependencies,
so nothing needs to be installed by hand.

- Install everything the project needs: `uv sync`
- Run the automated tests: `uv run pytest`
- Check the code style: `uv run ruff check .`

There's no single command to run a full scan yet, since delivery isn't
built. But you can see the pipeline produce a real (fake-data) report
right now:

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
today's date as the filename. This section will be updated with a
proper command once delivery is built.

## A note on safety

A lot of this project's design exists specifically to prevent mistakes:
things like never letting tests touch the internet or real market data,
requiring an explicit sign-off before the core pattern-detection logic
can change, and always sending a clear "scan failed" message rather than
a silently incomplete report. If any of that ever seems to be getting in
the way rather than helping, that's worth a conversation, not a
workaround.
