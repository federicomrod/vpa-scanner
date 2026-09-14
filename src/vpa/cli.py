"""Command-line entry points used by the supervisor script
(scripts/run_scan.sh). Not meant to be imported by application code -
see pipeline.py for that.

Deliberately split into two pieces:

- `build-report`: runs the pipeline once and writes the report to disk.
  Never sends anything anywhere, so scripts/run_scan.sh can safely
  retry it on a transient failure without risking a duplicate or
  premature "SCAN UNAVAILABLE" notification.
- `deliver`: sends the final email/push and pings the dead-man's-switch
  exactly once, after scripts/run_scan.sh has finished retrying
  `build-report`. On success it reads the report `build-report` already
  wrote back off disk; on failure it's given the error message
  directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from vpa.config import ConfigError, load_config
from vpa.delivery.deadman import DeadManConfigError, build_deadman_switch
from vpa.delivery.email import EmailConfigError, build_email_sender
from vpa.delivery.push import PushConfigError, build_push_sender
from vpa.pipeline import (
    REPO_ROOT,
    SIGNAL_DIR,
    PipelineError,
    resolve_output_dir,
    run_and_write_report,
    send_failure,
    send_success,
)

#: Where the real config.yaml lives. Overridable for tests.
CONFIG_PATH = REPO_ROOT / "config.yaml"


def build_report(
    *,
    config_path: Path = CONFIG_PATH,
    repo_root: Path = REPO_ROOT,
    signal_dir: Path = SIGNAL_DIR,
    report_date: date | None = None,
) -> int:
    """Run the pipeline once and write the report to disk.

    Prints the Markdown report's path on success. Exit 0 on success, 1
    on failure - this is the exit code scripts/run_scan.sh checks to
    decide whether to retry.
    """
    try:
        config = load_config(config_path)
        paths = run_and_write_report(
            config, repo_root=repo_root, signal_dir=signal_dir, report_date=report_date
        )
    except (ConfigError, PipelineError) as exc:
        print(f"Scan attempt failed: {exc}", file=sys.stderr)
        return 1
    print(paths.markdown_path)
    return 0


def deliver(
    argv: list[str],
    *,
    config_path: Path = CONFIG_PATH,
    repo_root: Path = REPO_ROOT,
    report_date: date | None = None,
) -> int:
    """Send the final email/push and ping the dead-man's-switch exactly
    once - `--ok` after `build-report` succeeded, `--failed MESSAGE`
    after every retry of `build-report` was exhausted.

    Exit 0 once delivered, 2 if delivery itself isn't configured (or,
    for `--ok`, if the report `build-report` should have written is
    missing).
    """
    parser = argparse.ArgumentParser(prog="vpa deliver")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ok", action="store_true", help="build-report succeeded; send the real report."
    )
    group.add_argument(
        "--failed", metavar="MESSAGE", help="build-report failed; MESSAGE explains why."
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(config_path)
        email_sender = build_email_sender(config.email)
        push_sender = build_push_sender(config.push)
        deadman = build_deadman_switch(config.deadman)
    except (ConfigError, EmailConfigError, PushConfigError, DeadManConfigError) as exc:
        print(f"Cannot deliver: {exc}", file=sys.stderr)
        return 2

    resolved_date = report_date or date.today()

    if args.failed is not None:
        send_failure(resolved_date, args.failed, email_sender, push_sender, deadman)
        return 0

    output_dir = resolve_output_dir(repo_root, config.report.output_dir)
    markdown_path = output_dir / f"{resolved_date.isoformat()}.md"
    json_path = output_dir / f"{resolved_date.isoformat()}.json"
    try:
        markdown_body = markdown_path.read_text()
        candidate_count = len(json.loads(json_path.read_text())["candidates"])
    except OSError as exc:
        print(
            f"Cannot deliver: report file missing ({exc}). Did build-report run first?",
            file=sys.stderr,
        )
        return 2

    send_success(resolved_date, candidate_count, markdown_body, email_sender, push_sender, deadman)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("Usage: python -m vpa.cli {build-report|deliver} ...", file=sys.stderr)
        return 64  # EX_USAGE

    command, rest = args[0], args[1:]
    if command == "build-report":
        return build_report()
    if command == "deliver":
        return deliver(rest)

    print(f"Unknown command: {command!r}", file=sys.stderr)
    return 64  # EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
