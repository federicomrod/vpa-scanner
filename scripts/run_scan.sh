#!/usr/bin/env bash
#
# The supervisor script: what a scheduled job (cron, launchd, ...) calls
# every morning. See README.md for the one-time setup this needs before
# it can run for real, and CLAUDE.md for the project's rules.
#
# What this does, in order:
#   1. Takes an exclusive lock so two scans can never run at once.
#   2. Loads secrets from a private environment file (never committed -
#      see CLAUDE.md; this script is the one place allowed to source
#      one directly, since that rule is about the Python codebase, not
#      this operational script).
#   3. Exits quietly if today isn't a trading day.
#   4. Refuses to run unless checked out at an exact git tag.
#   5. Attempts to build the report up to 3 times, with a timeout and
#      exponential backoff between attempts. This step never sends
#      anything anywhere, so a retry can never cause a duplicate or
#      misleading notification.
#   6. Sends exactly one final notification - the real report if any
#      attempt succeeded, or a clear "SCAN UNAVAILABLE" if none did -
#      and always pings the dead-man's-switch either way.
#
# Exit codes: 0 = delivered (success or a handled failure), or a quiet,
# expected no-op (already running, not a trading day). 1 = the scan
# itself failed after all retries. 2 = delivery isn't configured
# correctly (see README.md's "Setting up delivery"). Anything else is
# unexpected.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

LOCK_FILE="${VPA_LOCK_FILE:-$REPO_ROOT/.run_scan.lock}"
TIMEOUT_SECONDS="${VPA_TIMEOUT_SECONDS:-300}"
MAX_ATTEMPTS=3
INITIAL_BACKOFF_SECONDS=5

log() {
    echo "[run_scan.sh] $*"
}

# --- Tool checks -------------------------------------------------------------
# `flock` and `timeout` are standard on Linux but not built into macOS.
# On macOS: `brew install flock coreutils` (coreutils provides `timeout`
# as `gtimeout`, so we look for either name).
if ! command -v flock >/dev/null 2>&1; then
    log "flock is not installed. On macOS: brew install flock"
    exit 1
fi
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_BIN="gtimeout"
else
    log "Neither timeout nor gtimeout is installed. On macOS: brew install coreutils"
    exit 1
fi

# --- 1. Exclusive lock ---------------------------------------------------
# If another run is already in progress, back off quietly - it owns
# today's notification, so we don't send a second one.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Another run is already in progress. Exiting."
    exit 0
fi

# --- 2. Load secrets -------------------------------------------------------
if [[ -z "${VPA_ENV_FILE:-}" ]]; then
    log "VPA_ENV_FILE is not set - refusing to run without knowing where the secrets are."
    exit 1
fi
if [[ ! -f "$VPA_ENV_FILE" ]]; then
    log "VPA_ENV_FILE ($VPA_ENV_FILE) does not exist."
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$VPA_ENV_FILE"
set +a

# --- 3. Market calendar ---------------------------------------------------
if ! uv run python -m vpa.trading_calendar; then
    log "Not a trading day. Exiting quietly."
    exit 0
fi

# --- 4. Must be running from an exact git tag -------------------------------
if ! TAG="$(git describe --tags --exact-match 2>/dev/null)"; then
    log "Refusing to run: the checked-out commit is not an exact git tag."
    uv run python -m vpa.cli deliver --failed \
        "Refusing to run: not checked out at an exact git tag (see CLAUDE.md's change-control rules)."
    exit 1
fi
log "Running from tag: $TAG"

# --- 5. Build the report, retrying transient failures ------------------------
attempt=1
backoff="$INITIAL_BACKOFF_SECONDS"
build_succeeded=0
last_error=""

while [[ "$attempt" -le "$MAX_ATTEMPTS" ]]; do
    log "Build attempt $attempt of $MAX_ATTEMPTS..."
    if last_error="$("$TIMEOUT_BIN" "$TIMEOUT_SECONDS" uv run python -m vpa.cli build-report 2>&1 >/dev/null)"; then
        build_succeeded=1
        break
    fi
    log "Attempt $attempt failed: $last_error"
    if [[ "$attempt" -lt "$MAX_ATTEMPTS" ]]; then
        log "Retrying in ${backoff}s..."
        sleep "$backoff"
        backoff=$((backoff * 2))
    fi
    attempt=$((attempt + 1))
done

# --- 6. Deliver exactly once ------------------------------------------------
if [[ "$build_succeeded" -eq 1 ]]; then
    log "Build succeeded. Delivering the report."
    uv run python -m vpa.cli deliver --ok
    log "Done."
else
    log "All $MAX_ATTEMPTS attempts failed. Sending SCAN UNAVAILABLE."
    uv run python -m vpa.cli deliver --failed "$last_error"
    exit 1
fi
