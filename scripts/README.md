# scripts/

Operational scripts that are not part of the Python package itself.

`run_scan.sh` will live here starting in Milestone 1, step 5. It is the
script a scheduled job (e.g. cron) actually calls each morning: it takes
a lock so two scans can never overlap, checks the market calendar, and
runs the real pipeline with retries and a timeout.
