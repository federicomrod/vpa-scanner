# scripts/

Operational scripts that are not part of the Python package itself.

`run_scan.sh` will live here starting in Milestone 1, step 5. It is the
script a scheduled job (e.g. cron) actually calls each morning: it takes
a lock so two scans can never overlap, checks the market calendar, and
runs the real pipeline with retries and a timeout.

## Downloading market data (Milestone 2)

The data download is a Python command, not a script in this folder. It
reads `MASSIVE_API_KEY` from `~/vpa-secrets/.env` itself while it runs;
the key is never printed or logged. The project owner runs it, from the
repository folder.

**Small test run** (10 tickers, last 30 trading days - a few minutes):

    ~/.local/bin/uv run python -m vpa.data.ingest

**Market-cap look-ahead check** (10 tickers x 4 dates - under a minute):

    ~/.local/bin/uv run python -m vpa.data.check_market_cap

**Monthly universe snapshots** (e.g. two months, to test):

    ~/.local/bin/uv run python -m vpa.data.build_universe --first-month 2025-08 --last-month 2025-09

**Minute bars for every universe member, plus SPY** (after the
universe snapshots are built; hours - run in Terminal, see below):

    caffeinate -i ~/.local/bin/uv run python -m vpa.data.ingest_universe

Long runs: start them from a Terminal window rather than the Claude
chat (which gives up waiting after two minutes). `caffeinate -i` stops
the Mac sleeping until the command finishes. If a run stops for any
reason, start it again - anything already stored is skipped.

All of these print progress as they go and save a full log under
`~/vpa-data/logs/`. Downloaded data goes to `~/vpa-data/raw/` and is never
overwritten; running the download again only fetches what is missing.
Add `--help` to any command to see its options.
