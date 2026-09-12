"""FROZEN SPECIFICATION AREA.

This package implements Sections 2-13 of docs/concept-v2.md exactly as
written. It is empty in Milestone 1 on purpose: no pattern logic is built
here yet.

Before changing anything in this package (in any future milestone):

  1. Read CLAUDE.md at the repository root, in full.
  2. Confirm the change traces back to docs/concept-v2.md, or is an
     approved amendment to it.
  3. Add an experiment-ledger entry and reference it in the pull request
     description as `Ledger: LEDGER-<number>`.

A pull request that touches this package without a matching `Ledger:` line
in its description will be blocked automatically by the "Class 2 guard"
CI check (see .github/workflows/class2-guard.yml). This is intentional
and is not a bug to work around.

If a task seems to require touching this package as a side effect of
something else, stop and say so explicitly instead of proceeding.
"""
