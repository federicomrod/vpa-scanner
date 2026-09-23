"""The blind label tool - Concept v2 Section 12.

Ten charts a session, each truncated at the bar being judged, each
answered Interesting or Not interesting. About 30 sessions to reach the
300 labels Section 12 asks for.

    uv run python -m vpa.data.labels --labels-dir /Volumes/VPALabels/labels

### Run this from Terminal, not from an assistant session

The labels are the one measure in this project that is independent of
the system. If the assistant could run this tool it would see which
charts were drawn and which stratum each came from, and the blinding
would be its to break rather than the trader's to keep. `--labels-dir`
is therefore **required and has no default**: the path lives on the
encrypted volume and appears nowhere in this repository.

### What the browser is given

Only the chart, and only up to the bar being judged. The stratum and the
inverse-probability weight are written to the label file, never sent to
the page - so "which of these did the system flag?" cannot be answered
by looking at the page source, and the order is shuffled so it cannot be
answered by counting either.

Future price action is not withheld from the page; it is **never loaded
into it**. `vpa.data.charts` refuses bars past the truncation point.

### The file

One JSON object per line, appended, in `blind-labels.jsonl`. Append-only
so a session can never damage what earlier ones recorded.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from vpa.data.bars import DAILY, HOURLY, derived_path
from vpa.data.charts import chart_svg
from vpa.data.events import stored_sessions
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.signal.candidates import candidates
from vpa.signal.sampling import CHARTS_PER_SESSION, TARGET_LABELS, draw, sessions_to_target, split

LABEL_FILE = "blind-labels.jsonl"

#: How many hourly bars of history each chart shows behind the bar being
#: judged. Enough to read a stretch, not so much that the bar is a speck.
HISTORY_BARS = 60

RESPONSES = ("interesting", "not_interesting")


# --- choosing the charts ------------------------------------------------------


def eligible_bars(data_root: Path, session: date) -> pd.DataFrame:
    """Every universe bar on a session, marked with whether it fired."""
    parts = glob.glob(
        str(data_root / "derived/features/hourly" / f"date={session}" / "shard-*.parquet")
    )
    if not parts:
        return pd.DataFrame()
    features = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    prices = pd.read_parquet(
        derived_path(data_root, HOURLY, session),
        columns=["security_key", "slot_index", "open", "high", "low", "close"],
    )
    joined = features.merge(prices, on=["security_key", "slot_index"], how="left")
    marked = candidates(joined)
    return marked[["security_key", "requested_ticker", "date", "slot_index", "is_candidate"]]


def history_for(data_root: Path, key: str, session: date, slot: int) -> pd.DataFrame:
    """The bars behind one chart, ending **at** the bar being judged.

    Nothing after it is read, so nothing after it can be rendered.
    """
    sessions = stored_sessions(data_root)
    if session not in sessions:
        raise ValueError(
            f"{session} is not a stored session, so its history cannot be bounded. "
            "A chart must end exactly at the bar being judged."
        )
    at = sessions.index(session)
    window = sessions[max(0, at - HISTORY_BARS // 6) : at + 1]
    frames = []
    for day in window:
        path = derived_path(data_root, HOURLY, day)
        if not path.exists():
            continue
        rows = pd.read_parquet(
            path,
            columns=[
                "security_key",
                "date",
                "slot_index",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )
        mine = rows[rows["security_key"] == key]
        if day == session:
            mine = mine[mine["slot_index"] <= slot]
        frames.append(mine)
    if not frames:
        return pd.DataFrame()
    bars = pd.concat(frames, ignore_index=True).sort_values(["date", "slot_index"])
    return bars.tail(HISTORY_BARS).reset_index(drop=True)


def already_seen(labels: list[dict]) -> set[tuple[str, str, int]]:
    """Every bar that has been labelled already.

    Section 12 shows a chart twice only as a deliberate test-retest,
    after at least 28 days. An accidental repeat is not that: it is a
    duplicate that would look like agreement with himself and quietly
    inflate the consistency this is meant to measure.
    """
    return {
        (label["security_key"], str(label["date"]), int(label["slot_index"])) for label in labels
    }


def daily_history(data_root: Path, key: str, session: date, slot: int, count: int = 60):
    """Daily bars behind the chart, ending with a **partial** bar.

    A trader reading a chart infers support and resistance from where
    price has turned before, and sixty hourly bars is eight days -
    nowhere near enough to see a level that formed weeks ago. Section
    8.1 gives the AI "the preceding 60 daily bars"; the human judging
    the same bar should not be given less.

    **The current session's stored daily bar cannot be used.** It covers
    the whole day, so mid-session it contains hours that have not
    happened yet - the plainest kind of look-ahead. The last bar here is
    therefore built from the hourly bars up to and including the one
    being judged, and no further.
    """
    sessions = stored_sessions(data_root)
    if session not in sessions:
        raise ValueError(f"{session} is not a stored session")
    at = sessions.index(session)
    window = sessions[max(0, at - count) : at]

    frames = []
    for day in window:
        path = derived_path(data_root, DAILY, day)
        if not path.exists():
            continue
        rows = pd.read_parquet(
            path,
            columns=["security_key", "date", "open", "high", "low", "close", "volume"],
        )
        frames.append(rows[rows["security_key"] == key])

    partial = _partial_day(data_root, key, session, slot)
    if partial is not None:
        frames.append(partial)
    if not frames:
        return pd.DataFrame()
    bars = pd.concat(frames, ignore_index=True).sort_values("date")
    return bars.assign(slot_index=0).reset_index(drop=True)


def _partial_day(data_root: Path, key: str, session: date, slot: int):
    """The session so far, as one bar: open to the judged bar's close."""
    path = derived_path(data_root, HOURLY, session)
    if not path.exists():
        return None
    rows = pd.read_parquet(
        path,
        columns=["security_key", "date", "slot_index", "open", "high", "low", "close", "volume"],
    )
    so_far = rows[(rows["security_key"] == key) & (rows["slot_index"] <= slot)].sort_values(
        "slot_index"
    )
    if so_far.empty:
        return None
    return pd.DataFrame(
        [
            {
                "security_key": key,
                "date": session,
                "open": float(so_far.iloc[0]["open"]),
                "high": float(so_far["high"].max()),
                "low": float(so_far["low"].min()),
                "close": float(so_far.iloc[-1]["close"]),
                "volume": float(so_far["volume"].sum()),
            }
        ]
    )


def plan_session(
    data_root: Path,
    seed: str,
    count: int,
    sample_sessions: int = 40,
    seen: set[tuple[str, str, int]] | None = None,
) -> pd.DataFrame:
    """The charts for one labelling session, excluding anything already
    labelled."""
    sessions = stored_sessions(data_root)
    usable = sessions[120:]
    picked = usable[:: max(1, len(usable) // sample_sessions)][-sample_sessions:]
    frames = [eligible_bars(data_root, session) for session in picked]
    frames = [f for f in frames if not f.empty]
    if not frames:
        raise FileNotFoundError("No features stored; nothing to label.")
    pool = pd.concat(frames, ignore_index=True)
    if seen:
        keep = [
            (key, str(day), int(slot)) not in seen
            for key, day, slot in zip(
                pool["security_key"], pool["date"], pool["slot_index"], strict=True
            )
        ]
        pool = pool[keep]
    if pool.empty:
        raise FileNotFoundError("Every eligible bar has been labelled already.")
    return draw(split(pool), count, seed)


# --- the label file -----------------------------------------------------------


def read_labels(labels_dir: Path) -> list[dict]:
    path = labels_dir / LABEL_FILE
    if not path.exists():
        return []
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def append_label(labels_dir: Path, record: dict) -> None:
    labels_dir.mkdir(parents=True, exist_ok=True)
    with (labels_dir / LABEL_FILE).open("a") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


# --- the page -----------------------------------------------------------------

PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blind labels</title>
<style>
 body{{font:15px system-ui,sans-serif;margin:0;background:#fafafa;color:#1a1a1a}}
 header{{padding:10px 16px;background:#fff;border-bottom:1px solid #e6e6e6;
   display:flex;justify-content:space-between;align-items:baseline}}
 main{{max-width:940px;margin:0 auto;padding:16px}}
 .chart{{background:#fff;border:1px solid #e6e6e6;border-radius:8px;padding:8px}}
 .row{{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}}
 button{{font:600 15px system-ui;padding:12px 20px;border-radius:8px;border:1px solid #ccc;
   background:#fff;cursor:pointer}}
 button.yes{{border-color:#1a7f4b;color:#1a7f4b}}
 button.no{{border-color:#b3261e;color:#b3261e}}
 input,select{{font:15px system-ui;padding:10px;border:1px solid #ccc;border-radius:8px}}
 input{{flex:1;min-width:220px}}
 .muted{{color:#6b6b6b;font-size:13px}}
 .done{{text-align:center;padding:60px 16px}}
</style>
<header><strong>Blind labels</strong>
<span class="muted">{position} of {total} this session &middot; {stored} labelled overall</span>
</header>
<main>
 <div class="chart">{chart}</div>
 <p class="muted" style="margin:10px 0 4px">Daily, same stock, same moment</p>
 <div class="chart">{daily}</div>
 <form method="post" action="/answer">
  <div class="row">
   <button class="yes" name="response" value="interesting">Interesting</button>
   <button class="no" name="response" value="not_interesting">Not interesting</button>
  </div>
  <div class="row">
   <input name="note" placeholder="Optional note" autocomplete="off">
   <select name="pattern_guess">
    <option value="">Pattern guess (optional)</option>
    <option>absorption / support</option>
    <option>distribution / resistance</option>
    <option>effort without result</option>
    <option>climax</option>
    <option>no pattern</option>
   </select>
  </div>
 </form>
 <p class="muted">No future price action is loaded into this page.</p>
</main>"""

DONE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session complete</title>
<body style="font:16px system-ui;text-align:center;padding:60px">
<h2>Session complete</h2>
<p><strong>{labelled}</strong> labels stored in total.</p>
<p>{to_go} more labels to reach {target} &mdash; about {remaining} more sessions of ten.</p>
<p style="color:#6b6b6b">Remember to unmount the volume:<br>
<code>hdiutil detach /Volumes/VPALabels</code></p>"""


class Reviewer(BaseHTTPRequestHandler):
    charts: list[dict] = []
    labels_dir: Path = Path()
    data_root: Path = Path()
    position = 0
    already = 0

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path != "/":
            return self._send(404, "not found")
        if Reviewer.position >= len(Reviewer.charts):
            labelled = Reviewer.already + len(Reviewer.charts)
            return self._send(
                200,
                DONE.format(
                    labelled=labelled,
                    to_go=max(TARGET_LABELS - labelled, 0),
                    remaining=sessions_to_target(labelled),
                    target=TARGET_LABELS,
                ),
            )
        chart = Reviewer.charts[Reviewer.position]
        return self._send(
            200,
            PAGE.format(
                chart=chart["svg"],
                daily=chart["daily_svg"],
                position=Reviewer.position + 1,
                total=len(Reviewer.charts),
                stored=Reviewer.already + Reviewer.position,
            ),
        )

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        chart = Reviewer.charts[Reviewer.position]
        append_label(
            Reviewer.labels_dir,
            {
                "security_key": chart["security_key"],
                "date": chart["date"],
                "slot_index": int(chart["slot_index"]),
                # Recorded, never shown (Section 12).
                "stratum": chart["stratum"],
                "weight": float(chart["weight"]),
                "shown_on": date.today().isoformat(),
                "is_retest": bool(chart.get("is_retest", False)),
                "response": form.get("response", [""])[0],
                "note": form.get("note", [""])[0].strip(),
                "pattern_guess": form.get("pattern_guess", [""])[0],
                "answered_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        Reviewer.position += 1
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _send(self, code: int, body: str) -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # the terminal is for the trader, not for request logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.labels",
        description="Show charts for blind labelling (Concept v2 Section 12).",
    )
    parser.add_argument(
        "--labels-dir",
        type=Path,
        required=True,
        help="Where labels are stored, on the encrypted volume, "
        "e.g. /Volumes/VPALabels/labels. Required: it has no default on purpose.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--count", type=int, default=CHARTS_PER_SESSION)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--seed",
        help="Overrides the automatic one. Normally leave this alone: the "
        "automatic seed changes with every session, so running twice in one "
        "day draws different charts.",
    )
    args = parser.parse_args(argv)

    if not args.labels_dir.parent.exists():
        print(f"{args.labels_dir.parent} is not there. Is the volume mounted?")
        print("  hdiutil attach ~/vpa-labels.sparsebundle")
        return 1

    stored = read_labels(args.labels_dir)
    already = len(stored)
    print(f"{already} labels stored. {max(TARGET_LABELS - already, 0)} to go "
          f"({sessions_to_target(already)} more sessions of {args.count}).")  # fmt: skip

    # The seed moves with the number of labels already stored, so a
    # second session on the same day is a different draw. Without this,
    # the date alone would hand back the same ten charts.
    seed = args.seed or f"{date.today().isoformat()}-{already}"
    planned = plan_session(args.data_root, seed, args.count, seen=already_seen(stored))
    charts = []
    for row in planned.itertuples(index=False):
        bars = history_for(args.data_root, row.security_key, row.date, int(row.slot_index))
        if bars.empty:
            continue
        wider = daily_history(args.data_root, row.security_key, row.date, int(row.slot_index))
        charts.append(
            {
                "svg": chart_svg(bars, "hourly"),
                "daily_svg": chart_svg(wider, "daily") if not wider.empty else "",
                "security_key": row.security_key,
                "date": row.date,
                "slot_index": row.slot_index,
                "stratum": row.stratum,
                "weight": row.weight,
            }
        )
    if not charts:
        print("No charts could be drawn.")
        return 1

    Reviewer.charts = charts
    Reviewer.labels_dir = args.labels_dir
    Reviewer.data_root = args.data_root
    Reviewer.position = 0
    Reviewer.already = already

    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n{len(charts)} charts ready. Open {url}")
    print("Press Ctrl-C when you are done.\n")
    webbrowser.open(url)
    server = HTTPServer(("127.0.0.1", args.port), Reviewer)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopped. {Reviewer.already + Reviewer.position} labels stored in total.")
        print("Remember: hdiutil detach /Volumes/VPALabels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
