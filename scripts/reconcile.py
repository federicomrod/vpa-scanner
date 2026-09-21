#!/usr/bin/env python
"""Reconciliation report (Concept v2 Section 3.3).

A thin wrapper so the report can be run as `scripts/reconcile.py`; the
code lives in `vpa.data.reconcile` where it can be tested.
"""

import sys

from vpa.data.reconcile import main

if __name__ == "__main__":
    sys.exit(main())
