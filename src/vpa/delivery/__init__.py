"""Sending the report out: email, a push notification, and pinging a
dead-man's-switch monitoring service.

Every sender here is a small class with a `.send()` (or `.ping_*()`)
method, so tests can swap in a fake that just records what would have
been sent instead of a real one that talks to the network - see
CLAUDE.md, rule 4 (no network calls in tests, ever).
"""
