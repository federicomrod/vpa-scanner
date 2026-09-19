"""Data loading, storage and access.

- `ingest.py` downloads raw market data from Massive into the write-once
  raw store (`raw_store.py`) under `~/vpa-data/raw/`. It is the production
  data loader CLAUDE.md rule 3 refers to, and the only code that reads the
  secrets file (`secrets.py`, `~/vpa-secrets/.env`) - approved by the
  project owner. It is run by the owner, never by tests or by Claude.
- `build_universe.py` builds the monthly universe snapshots from
  whole-market data (`universe_inputs.py`); `universe.py` stores them.
- `calendar.py` is the NYSE exchange calendar.
- `fake.py` is the Milestone 1 fake-data stand-in.

Nothing else here may read `.env` files, `holdout/`, or `data/raw/`.
"""
