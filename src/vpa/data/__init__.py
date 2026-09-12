"""Data loading and access.

Will hold code that loads market data for the signal package to analyse.
There is no real market data source yet — `fake.py` stands in for one,
producing clearly-labelled fake rows so the rest of the pipeline can be
built and tested. None of this package may read from `.env`, `holdout/`,
or `data/raw/` — see CLAUDE.md.
"""
