# fixtures/

Small, fake sample data files used by the automated tests. Nothing in
here is real market data - it exists so tests can run without a network
connection and without touching `data/raw/` or `holdout/` (which tests
are never allowed to read - see CLAUDE.md).

## universe/

`universe/securities.csv` is a hand-written FAKE list of made-up stocks
(every ticker starts with `FAKE`) used to test the universe rules in
Concept v2 Section 2. Each row is designed to test one rule, and the
`selected` and `why` columns say what should happen to it. The tests
turn each row into 60+ days of identical daily bars at the given
`close` and `volume`. Market cap = `share_class_shares` x `close`.
