#!/usr/bin/env python3
"""Report on the nightly EPG wake run: did the zap tour collect what it should?

Reads the journal (no sudo needed — the unit logs are world-readable here),
groups the structured log lines into runs, and compares the most recent run
against the ones before it. The point is to catch a tour that quietly stopped
collecting: a transponder that exits at the dwell floor with a fraction of its
usual event count means the dwell bounds need revisiting, and nothing else in
the system would notice.

    prime_report.py            # print the report
    prime_report.py --mail     # e-mail it (used by cron at 05:00)
    prime_report.py --days 14  # widen the history window
"""
from __future__ import annotations
import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime

UNIT = "tv-tipps"
# Below this fraction of a transponder's historical median we call it a drop
# worth reporting rather than normal night-to-night variation.
DROP_RATIO = 0.5


def journal_events(days: int) -> list[dict]:
    """Structured log lines from the unit, oldest first."""
    out = subprocess.run(
        ["journalctl", "-u", UNIT, "--since", f"-{days}d", "--no-pager", "-o", "cat"],
        capture_output=True, text=True, check=False,
    ).stdout
    events = []
    for line in out.splitlines():
        i = line.find('{"')
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except ValueError:
            continue
        if isinstance(d, dict) and "event" in d:
            events.append(d)
    return events


def group_runs(events: list[dict]) -> list[dict]:
    """Split the log into tour runs. A run opens at prime_start and carries
    whatever followed it until the next one."""
    runs: list[dict] = []
    for d in events:
        ev = d.get("event", "")
        if ev == "epg.prime_start":
            runs.append({"start": d.get("timestamp"), "cfg": d, "tps": [],
                         "done": None, "sweep": None, "now_next": None,
                         "wake_failed": [], "left_on": False})
        if not runs:
            continue
        run = runs[-1]
        if ev == "epg.prime_transponder":
            run["tps"].append(d)
        elif ev == "epg.prime_done":
            run["done"] = d
        elif ev == "epg.full_refresh_done":
            run["sweep"] = d
        elif ev == "epg.refreshed_now_next":
            run["now_next"] = d
        elif ev == "epg.wake_failed":
            run["wake_failed"].append(d)
        elif ev == "epg.wake_sleep_back":
            run["left_on"] = not d.get("ok", False)
    return runs


def _local(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone().strftime("%Y-%m-%d %H:%M"))
    except ValueError:
        return ts


def report(runs: list[dict]) -> str:
    if not runs:
        return f"No EPG wake run found in the journal for unit {UNIT}."

    latest = runs[-1]
    history = runs[:-1]
    cfg = latest["cfg"]
    floor = cfg.get("min_sec")
    lines: list[str] = []

    done = latest["done"] or {}
    lines.append(f"EPG wake run {_local(latest['start'])} on {cfg.get('receiver')}")
    lines.append("")
    lines.append(f"  transponders visited : {done.get('visited')}/{done.get('transponders')}")
    lines.append(f"  tour duration        : {done.get('total_sec')}s")
    lines.append(f"  slowest saturation   : {done.get('slowest_saturation_sec')}s")
    lines.append(f"  hit the ceiling      : {done.get('hit_ceiling')}")
    lines.append(f"  bounds               : floor {floor}s / flat {cfg.get('flat_sec')}s"
                 f" / ceiling {cfg.get('max_sec')}s")
    if latest["now_next"]:
        lines.append(f"  now/next events      : {latest['now_next'].get('events')}")
    if latest["sweep"]:
        lines.append(f"  sweep skipped        : {latest['sweep'].get('skipped')} channels")

    # Per-transponder history, so a drop stands out against its own baseline.
    past: dict[str, list[int]] = {}
    for run in history:
        for tp in run["tps"]:
            past.setdefault(tp.get("transponder"), []).append(tp.get("events", 0))

    lines.append("")
    lines.append(f"  {'transponder':16} {'ch':>2} {'events':>7} {'median':>7} {'sat':>5} {'dwell':>5}")
    alerts: list[str] = []
    for tp in sorted(latest["tps"], key=lambda t: -(t.get("events") or 0)):
        key = tp.get("transponder")
        seen = past.get(key, [])
        med = round(statistics.median(seen)) if seen else None
        events = tp.get("events") or 0
        sat = tp.get("saturated_after_sec")
        dwell = tp.get("dwell_sec")
        mark = ""
        if med and events < med * DROP_RATIO:
            mark = "  <-- DROP"
            alerts.append(f"{key} ({tp.get('channel')}): {events} events vs median {med}"
                          f", saturated after {sat}s, dwell {dwell}s")
        if tp.get("reason") == "ceiling":
            mark = "  <-- CEILING"
            alerts.append(f"{key} ({tp.get('channel')}): still growing at the "
                          f"{cfg.get('max_sec')}s ceiling — raise it")
        lines.append(f"  {str(key):16} {tp.get('channels'):2} {events:7} "
                     f"{str(med) if med is not None else '-':>7} {str(sat):>5} {str(dwell):>5}{mark}")

    # A tour that exits at the floor everywhere means the floor is doing the
    # deciding, not the data — the bounds want revisiting either way.
    at_floor = [t for t in latest["tps"] if t.get("dwell_sec") == floor]
    lines.append("")
    lines.append(f"  exited at the {floor}s floor: {len(at_floor)}/{len(latest['tps'])}")

    if latest["wake_failed"]:
        alerts.append(f"wake failed: {[w.get('reason') for w in latest['wake_failed']]}")
    if latest["left_on"]:
        alerts.append("RECEIVER MAY STILL BE POWERED ON — sleep_receiver reported failure")
    if done.get("unvisited"):
        alerts.append(f"{done['unvisited']} transponders unvisited (tour budget exhausted)")

    lines.append("")
    if alerts:
        lines.append("Needs attention:")
        lines += [f"  - {a}" for a in alerts]
    else:
        lines.append("Nothing needs attention: no drops, no ceilings, box powered back down.")

    if history:
        lines.append("")
        lines.append(f"Compared against {len(history)} earlier run(s), most recent "
                     f"{_local(history[-1]['start'])}.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="journal window (default 7)")
    ap.add_argument("--mail", action="store_true", help="e-mail the report")
    args = ap.parse_args()

    runs = group_runs(journal_events(args.days))
    text = report(runs)
    print(text)

    if args.mail:
        subject = "tv-tipps EPG wake report"
        if "Needs attention:" in text:
            subject += " — needs attention"
        subprocess.run(["/home/joachim/bin/send-mail.py", subject],
                       input=text, text=True, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
