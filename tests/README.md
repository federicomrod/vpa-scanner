# tests/

Automated tests for this project, run with `pytest`.

Two rules that apply to every test in this folder:

1. **No test may make a network call.** Ever. Anything that would
   normally talk to the internet (sending an email, calling a webhook,
   pinging a URL) must be replaced with a fake/injectable version in
   tests. See CLAUDE.md at the repository root for why.
2. Tests use the sample data in `fixtures/`, never real market data.

Run them with `uv run pytest`.
