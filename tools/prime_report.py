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
DB_PATH = "/var/lib/tv-tipps/tv_tipps.db"
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
                         "wake_failed": [], "left_on": False, "claims": []})
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
        elif ev == "epg.wake_left_on_user_active":
            run["claims"].append(d.get("claim") or "user")
    return runs


# Everything that explains why a night produced no tour, newest last.
_ATTEMPT_EVENTS = {
    "power.intertechno": "mains switched {on}",
    "power.switch_retry": "gateway refused the command, retrying",
    "power.wake_resent": "on-command resent after {after_sec}s",
    "power.epg_wake_timeout": "receiver never came up ({timeout_sec}s)",
    "epg.wake_failed": "wake failed: {reason}",
    "epg.wake_failed_powered_down": "mains switched back off",
    "epg.wake_skip_online": "skipped: receiver was already on",
    "epg.prime_aborted_user_active": "tour aborted: {claim} took the box",
}


def _since(events: list[dict], ts: str | None) -> list[dict]:
    """Events newer than `ts` (an ISO timestamp), or all of them if ts is None."""
    if not ts:
        return events
    return [d for d in events if (d.get("timestamp") or "") > ts]


def epg_freshness() -> list[str]:
    """What the missed run actually costs, straight from the database."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            fav = """
                with fav as (
                  select distinct bc.channel_id from bouquet_channels bc
                  join bouquets b on b.id = bc.bouquet_id
                  where lower(b.name) like '%favorit%'
                     or lower(b.name) like '%favourite%'
                     or lower(b.name) like '%favorite%'),
                per as (
                  select e.channel_id cid,
                         julianday(max(e.start_time)) - julianday('now') d
                  from epg_events e join fav on fav.channel_id = e.channel_id
                  group by e.channel_id)
                select count(*), sum(case when d < 1 then 1 else 0 end),
                       round(min(d), 1), round(max(d), 1) from per"""
            n, dry, lo, hi = con.execute(fav).fetchone()
            newest = con.execute("select max(cached_at) from epg_events").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # a report must never fail on its own diagnostics
        return [f"  EPG state unavailable: {e}"]
    return [
        f"  EPG last written    : {newest} UTC",
        f"  favourite channels  : {n} with EPG, horizon {lo}-{hi} days",
        f"  running dry in 24 h : {dry or 0}",
    ]


def outcome_report(runs: list[dict], events: list[dict], hours: int) -> str:
    """What to send when no tour ran. Repeating yesterday's transponder table
    would bury the one thing worth knowing: the EPG did not get refreshed."""
    latest = runs[-1] if runs else None
    recent = _since(events, latest["start"] if latest else None)

    told: list[str] = []
    for d in recent:
        tmpl = _ATTEMPT_EVENTS.get(d.get("event", ""))
        if not tmpl:
            continue
        fmt = dict(d)
        fmt["on"] = "on" if d.get("on") else "off"   # the log field is a bool
        try:
            detail = tmpl.format(**fmt)
        except (KeyError, IndexError):
            detail = tmpl
        told.append(f"  {_local(d.get('timestamp'))}  {detail}")

    skipped = any(d.get("event") == "epg.wake_skip_online" for d in recent)
    headline = ("EPG wake run SKIPPED — the receiver was already on"
                if skipped else
                f"EPG wake run FAILED — no tour in the last {hours} h")

    lines = [headline, ""]
    if latest:
        lines.append(f"  last successful tour: {_local(latest['start'])}"
                     f" ({len(latest['tps'])} transponders)")
    else:
        lines.append("  last successful tour: none in the journal window")
    lines += epg_freshness()
    lines.append("")
    if told:
        lines.append("What the box did instead:")
        lines += told
    else:
        lines.append("Nothing in the journal — the scheduled job did not run at all."
                     " Check that the service was up at 03:30.")
    lines.append("")
    lines.append("Needs attention:")
    if skipped:
        lines.append("  - tour skipped by design; EPG only got what the box already had")
    else:
        lines.append("  - no EPG collected tonight; the data above ages by one more day")
    return "\n".join(lines)


def _local(ts: str | None) -> str:
    if not ts:
        return "?"
    try:
        return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone().strftime("%Y-%m-%d %H:%M"))
    except ValueError:
        return ts


def report(runs: list[dict], events: list[dict], hours: int = 24) -> str:
    from datetime import timedelta, timezone

    latest = runs[-1] if runs else None
    stale = True
    if latest and latest.get("start"):
        try:
            started = datetime.fromisoformat(latest["start"].replace("Z", "+00:00"))
            stale = datetime.now(timezone.utc) - started > timedelta(hours=hours)
        except ValueError:
            stale = False
    if stale:
        # A run older than the window means last night produced nothing. Say so
        # instead of re-sending numbers the user already read yesterday.
        return outcome_report(runs, events, hours)

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
    if done.get("aborted"):
        alerts.append(f"tour aborted after {done.get('visited')} transponders — "
                      "someone took the box over")
    elif done.get("unvisited"):
        alerts.append(f"{done['unvisited']} transponders unvisited (tour budget exhausted)")
    for claim in latest["claims"]:
        alerts.append(f"left powered on: {claim} was using it at shutdown time")

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

    events = journal_events(args.days)
    text = report(group_runs(events), events)
    print(text)

    if args.mail:
        subject = "tv-tipps EPG wake report"
        if "FAILED" in text:
            subject += " — FAILED"
        elif "Needs attention:" in text:
            subject += " — needs attention"
        subprocess.run(["/home/joachim/bin/send-mail.py", subject],
                       input=text, text=True, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
