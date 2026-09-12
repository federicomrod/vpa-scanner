# fixtures/

Small, fake sample data files used by the automated tests. Nothing in
here is real market data - it exists so tests can run without a network
connection and without touching `data/raw/` or `holdout/` (which tests
are never allowed to read - see CLAUDE.md).
