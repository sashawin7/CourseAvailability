#!/usr/bin/env python3
"""
gw_seat_watch.py — scrape GW's Schedule of Classes and report the status
(OPEN / WAITLIST / CLOSED / CANCELLED) of a course and its linked sections.

Usage:
    python gw_seat_watch.py                      # one-shot report for MATH 2184
    python gw_seat_watch.py --course 2184        # same
    python gw_seat_watch.py --subject CSCI --course 1311
    python gw_seat_watch.py --json               # machine-readable output
    python gw_seat_watch.py --watch 300          # re-check every 5 min, alert on change

Exit code is 0 if any linked (recitation/lab) section is OPEN, 1 otherwise —
handy if you want to chain it into something else.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://my.gwu.edu/mod/pws/courses.cfm"
HEADERS = {
    # Be a polite citizen: identify yourself and don't hammer the registrar.
    "User-Agent": "gw-seat-watch/1.0 (personal course-status checker)"
}

STATUSES = ("OPEN", "CLOSED", "WAITLIST", "CANCELLED", "TBA")

# A section row renders as one line of text, e.g.
#   OPEN 53899 MATH 2184 30 Recitation 0.00 Won, R TOMP 307 F 08:00AM - 08:50AM ...
# The site splits these across <td>s inconsistently (subject number is a link,
# cancelled rows drop cells), so match on the normalized row text instead of
# relying on fixed column positions.
ROW_RE = re.compile(
    r"^(?P<status>" + "|".join(STATUSES) + r")\s+"
    r"(?P<crn>\d{4,6})\s+"
    r"(?P<subject>[A-Z]{2,5})\s*"
    r"(?P<number>\d{4}[A-Z]?)\s+"
    r"(?P<section>[\w.]+)\s+"
    r"(?P<rest>.*)$"
)

# Days and start/end time always appear adjacent ("TR 12:45PM - 02:00PM").
# Matching them as one unit avoids grabbing a nested linked row's meeting info.
MEETING_RE = re.compile(
    r"\b(?P<days>[MTWRFSU]{1,5})\s+"
    r"(?P<time>\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M)"
)
CREDIT_RE = re.compile(r"\b(\d+\.\d{2})\b")
PAGE_RE = re.compile(r"goToPage\('(\d+)'\)")

LINKED_WORDS = ("recitation", "lab", "laboratory", "discussion")


def fetch_pages(subject, term, camp, session=None):
    """Fetch page 1, discover how many pages exist, then fetch the rest."""
    s = session or requests.Session()
    params = {"campId": camp, "termId": term, "subjId": subject}
    first = s.get(BASE, params=params, headers=HEADERS, timeout=30)
    first.raise_for_status()
    html = [first.text]

    pages = {int(n) for n in PAGE_RE.findall(first.text)}
    for n in sorted(p for p in pages if p > 1):
        r = s.get(BASE, params={**params, "pageNum": n}, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html.append(r.text)
        time.sleep(1)  # don't hammer
    return html


def parse_sections(html_pages, subject, number):
    """Return section dicts in document order, deduped by CRN."""
    sections, seen = [], set()

    for html in html_pages:
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            # Collapse the row (and anything nested in it) to a single line.
            text = " ".join(tr.get_text(" ", strip=True).split())
            m = ROW_RE.match(text)
            if not m:
                continue
            if m.group("subject") != subject or m.group("number") != number:
                continue
            crn = m.group("crn")
            if crn in seen:
                continue  # outer container row already yielded this one
            seen.add(crn)

            rest = m.group("rest")
            cm = CREDIT_RE.search(rest)
            credit = float(cm.group(1)) if cm else None
            # Title is everything before the credit figure. (Note: 0.00 is a
            # valid credit value and falsy -- test against None, not truthiness.)
            title = rest[: cm.start()].strip() if cm else rest.strip()
            # First meeting block in the row is this section's own.
            meeting = MEETING_RE.search(rest)

            sections.append(
                {
                    "status": m.group("status"),
                    "crn": crn,
                    "course": f"{subject} {number}",
                    "section": m.group("section"),
                    "title": title,
                    "credit": credit,
                    "days": meeting.group("days") if meeting else "",
                    "time": meeting.group("time") if meeting else "",
                    "is_linked": (credit == 0.0)
                    or any(w in title.lower() for w in LINKED_WORDS),
                }
            )
    return sections


def group_by_parent(sections):
    """Attach each 0-credit linked section to the lecture row above it."""
    groups, current = [], None
    for sec in sections:
        if sec["is_linked"] and current is not None:
            current["linked"].append(sec)
        else:
            current = {"parent": sec, "linked": []}
            groups.append(current)
    return groups


def render(groups):
    lines = []
    for g in groups:
        p = g["parent"]
        lines.append(
            f"{p['course']}.{p['section']}  [{p['status']}]  CRN {p['crn']}  "
            f"{p['title']}  {p['days']} {p['time']}".rstrip()
        )
        for s in g["linked"]:
            flag = "  <-- OPEN" if s["status"] == "OPEN" else ""
            lines.append(
                f"    .{s['section']}  [{s['status']:<9}] CRN {s['crn']}  "
                f"{s['days']} {s['time']}{flag}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def snapshot(sections):
    """Comparable state for change detection."""
    return {s["crn"]: s["status"] for s in sections}


def notify(message):
    print("\a" + message, flush=True)  # terminal bell
    # Optional desktop notification; silently ignored if unavailable.
    import shutil
    import subprocess

    try:
        if shutil.which("osascript"):
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "GW Seat Watch"'],
                check=False,
            )
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "GW Seat Watch", message], check=False)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subject", default="MATH")
    ap.add_argument("--course", default="2184", help="4-digit course number")
    ap.add_argument("--term", default="202603", help="e.g. 202603 = Fall 2026")
    ap.add_argument("--camp", default="1", help="1 = Main Campus")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="poll every N seconds and alert when a status changes (min 60)")
    args = ap.parse_args()

    def check():
        pages = fetch_pages(args.subject, args.term, args.camp)
        secs = parse_sections(pages, args.subject.upper(), args.course)
        if not secs:
            print(f"No sections found for {args.subject} {args.course} in term {args.term}.",
                  file=sys.stderr)
        return secs

    if not args.watch:
        secs = check()
        if args.json:
            print(json.dumps(secs, indent=2))
        else:
            print(f"{args.subject} {args.course} — checked {datetime.now():%Y-%m-%d %H:%M}\n")
            print(render(group_by_parent(secs)))
        return 0 if any(s["is_linked"] and s["status"] == "OPEN" for s in secs) else 1

    interval = max(60, args.watch)
    prev = None
    print(f"Watching {args.subject} {args.course} every {interval}s. Ctrl-C to stop.\n")
    while True:
        try:
            secs = check()
            now = snapshot(secs)
            if prev is None:
                print(render(group_by_parent(secs)))
            else:
                changed = [c for c in now if prev.get(c) != now[c]]
                for crn in changed:
                    sec = next(s for s in secs if s["crn"] == crn)
                    msg = (f"{sec['course']}.{sec['section']} (CRN {crn}): "
                           f"{prev.get(crn)} -> {now[crn]}")
                    notify(msg)
                if not changed:
                    print(f"[{datetime.now():%H:%M}] no change", flush=True)
            prev = now
        except requests.RequestException as e:
            print(f"[{datetime.now():%H:%M}] fetch failed: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
