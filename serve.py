#!/usr/bin/env python3
"""Dashboard server: serves dashboard.html and /api/state from pipeline.db.

Read-only. All metrics are derived at request time from the observations
table (latest row per entity = current state; the append trail = history).

Usage:
    python3 serve.py            # http://localhost:8400
    PIPELINE_DB=... DASH_PORT=... ACU_USD=... python3 serve.py
"""

import json
import os
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PIPELINE_DB", os.path.join(BASE_DIR, "pipeline.db"))
PORT = int(os.environ.get("DASH_PORT", "8400"))
POLL_INTERVAL = int(os.environ.get("GH_TRACK_POLL_INTERVAL", "300"))
ACU_USD = float(os.environ.get("ACU_USD", "0"))  # 0 = unset; UI shows ACUs only

LIFECYCLE_PRIORITY = [
    "devin-error", "devin-abandoned", "devin-needs-review",
    "devin-auto-ok", "devin-pr", "devin-hold", "devin", "devin-triage",
]
LIVE_SESSION = lambda s: s.get("status") in ("running", "suspended")
SESSION_URL_RE = re.compile(r"https://app\.devin\.ai/sessions/([0-9a-f]+)")
PR_URL_RE = re.compile(r"/pull/(\d+)$")


def iso(ts) -> str | None:
    """Normalize GitHub ISO strings and Devin epoch seconds to ISO UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return ts


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def day_of(s: str | None) -> str | None:
    dt = parse_dt(s)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d") if dt else None


def median_hours(deltas: list[float]) -> float | None:
    return round(statistics.median(deltas), 1) if deltas else None


def load(db: sqlite3.Connection):
    """Latest payload per entity, plus full session history for burn charts."""
    latest = {"issue": {}, "pr": {}, "session": {}, "timeline_event": {},
              "pr_detail": {}, "session_messages": {}}
    session_history = {}  # session_id -> [(fetched_at, payload)]
    for fetched_at, kind, key, payload_json in db.execute(
        "SELECT fetched_at, kind, entity_key, payload_json FROM observations ORDER BY id"
    ):
        payload = json.loads(payload_json)
        latest.setdefault(kind, {})[key] = payload
        if kind == "session":
            session_history.setdefault(key, []).append((fetched_at, payload))
    last_poll = db.execute(
        "SELECT started_at, finished_at, ok, notes FROM polls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return latest, session_history, last_poll


def build_state() -> dict:
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        latest, session_history, last_poll = load(db)
    finally:
        db.close()

    issues = {int(k): v for k, v in latest["issue"].items()}
    prs = {int(k): v for k, v in latest["pr"].items()}
    sessions = latest["session"]
    events = list(latest["timeline_event"].values())
    pr_details = {int(k): v for k, v in latest["pr_detail"].items()}
    session_msgs = latest["session_messages"]

    # --- joins ---------------------------------------------------------
    # session -> issue: tag issue:<n>, else "Devin session started" comment.
    session_issue = {}
    for sid, s in sessions.items():
        for tag in s.get("tags", []):
            if tag.startswith("issue:"):
                session_issue[sid] = int(tag.split(":", 1)[1])
    comment_links = {}  # session_id -> issue number, from tracker comments
    for e in events:
        if e.get("event") == "commented":
            m = SESSION_URL_RE.search(e.get("body") or "")
            if m:
                comment_links[m.group(1)] = e["issue_number"]
    for sid, n in comment_links.items():
        session_issue.setdefault(sid, n)
    # last resort: the tracker titles sessions "<owner>/<repo> issue #<n>: ..."
    title_re = re.compile(r"\bissue #(\d+):")
    for sid, s in sessions.items():
        if sid not in session_issue:
            m = title_re.search(s.get("title") or "")
            if m:
                session_issue[sid] = int(m.group(1))

    issue_sessions = {}  # number -> [session dicts], newest first
    for sid, n in session_issue.items():
        if sid in sessions:
            issue_sessions.setdefault(n, []).append(sessions[sid])
    for lst in issue_sessions.values():
        lst.sort(key=lambda s: s.get("created_at") or 0, reverse=True)

    # session -> PR numbers (from Devin's own pull_requests field)
    def session_pr_numbers(s):
        nums = []
        for p in s.get("pull_requests") or []:
            m = PR_URL_RE.search(p.get("pr_url") or "")
            if m:
                nums.append(int(m.group(1)))
        return nums

    issue_prs = {}  # number -> [pr numbers]
    for sid, n in session_issue.items():
        for pr_n in session_pr_numbers(sessions[sid]):
            issue_prs.setdefault(n, [])
            if pr_n not in issue_prs[n]:
                issue_prs[n].append(pr_n)

    devin_pr_numbers = {
        pr_n for s in sessions.values() for pr_n in session_pr_numbers(s)
    } | {
        n for n, p in prs.items()
        if (p.get("user") or {}).get("login", "").endswith("[bot]")
        and (p.get("head") or {}).get("ref", "").startswith("devin/")
    }

    # --- per-issue rows --------------------------------------------------
    def lifecycle_of(issue):
        names = [l.get("name") for l in issue.get("labels", [])]
        found = [n for n in LIFECYCLE_PRIORITY if n in names]
        return found[0] if found else None, [n for n in names if n and n.startswith("devin")]

    rows = []
    for n, issue in sorted(issues.items()):
        lifecycle, devin_labels = lifecycle_of(issue)
        sess = (issue_sessions.get(n) or [None])[0]
        linked_prs = [prs[p] for p in issue_prs.get(n, []) if p in prs]
        merged_pr = next((p for p in linked_prs if p.get("merged_at")), None)
        open_pr = next((p for p in linked_prs if p.get("state") == "open"), None)
        best_pr = merged_pr or open_pr or (linked_prs[0] if linked_prs else None)
        rows.append({
            "number": n,
            "title": issue.get("title", ""),
            "url": issue.get("html_url", ""),
            "state": issue.get("state"),
            "created_at": issue.get("created_at"),
            "closed_at": issue.get("closed_at"),
            "lifecycle": lifecycle,
            "devin_labels": devin_labels,
            "session": None if not sess else {
                "id": sess.get("session_id"),
                "url": sess.get("url"),
                "status": sess.get("status"),
                "status_detail": sess.get("status_detail"),
                "acus": sess.get("acus_consumed") or 0,
                "created_at": iso(sess.get("created_at")),
                "updated_at": iso(sess.get("updated_at")),
            },
            "pr": None if not best_pr else {
                "number": best_pr.get("number"),
                "url": best_pr.get("html_url"),
                "state": "merged" if best_pr.get("merged_at") else best_pr.get("state"),
                "created_at": best_pr.get("created_at"),
                "merged_at": best_pr.get("merged_at"),
            },
        })

    # --- funnel ----------------------------------------------------------
    tracked = [r for r in rows if r["lifecycle"] or r["session"]]
    spawned = [r for r in rows if r["session"]]
    with_pr = [r for r in rows if r["pr"]]
    merged = [r for r in rows if r["pr"] and r["pr"]["state"] == "merged"]
    funnel = {
        "opened": len(rows),
        "spawned": len(spawned),
        "pr_opened": len(with_pr),
        "merged": len(merged),
        "hold": sum(1 for r in rows if r["lifecycle"] == "devin-hold"),
        "error": sum(1 for r in rows if r["lifecycle"] == "devin-error"),
        "abandoned": sum(1 for r in rows if r["lifecycle"] == "devin-abandoned"),
    }

    # --- drift -----------------------------------------------------------
    drift = []
    for r in rows:
        n = r["number"]
        if r["lifecycle"] and r["lifecycle"] not in ("devin-hold", "devin-triage") and not r["session"]:
            drift.append({"issue": n, "kind": "label-no-session",
                          "detail": f"label {r['lifecycle']} but no Devin session found"})
        if r["session"] and not r["lifecycle"] and r["state"] == "open":
            drift.append({"issue": n, "kind": "session-no-label",
                          "detail": "Devin session exists but the issue has no devin-* label"})
        if r["session"] and r["state"] == "closed" and LIVE_SESSION(r["session"]):
            drift.append({"issue": n, "kind": "live-session-closed-issue",
                          "detail": f"issue closed but session {r['session']['status']}"})
        if r["lifecycle"] == "devin-pr" and r["pr"] and r["pr"]["state"] == "closed":
            drift.append({"issue": n, "kind": "pr-closed-label-stale",
                          "detail": "label devin-pr but the PR was closed unmerged"})
        if len(r["devin_labels"]) > 1:
            drift.append({"issue": n, "kind": "multiple-lifecycle-labels",
                          "detail": f"labels: {', '.join(r['devin_labels'])}"})
        if r["session"] and r["session"]["status_detail"] == "waiting_for_user" and r["state"] == "open":
            drift.append({"issue": n, "kind": "waiting-for-user",
                          "detail": "session is waiting for user input"})

    # open Devin PRs, directly from PR entities (catches PRs whose session
    # was never linked to an issue), with the issue join where known
    pr_issue = {}
    for n, pr_nums in issue_prs.items():
        for pr_n in pr_nums:
            pr_issue.setdefault(pr_n, n)
    open_prs = []
    for pr_n in sorted(devin_pr_numbers):
        pr = prs.get(pr_n)
        if not pr or pr.get("state") != "open" or pr.get("merged_at"):
            continue
        linked = pr_issue.get(pr_n)
        linked_row = next((r for r in rows if r["number"] == linked), None) if linked else None
        open_prs.append({
            "number": pr_n,
            "title": pr.get("title", ""),
            "url": pr.get("html_url"),
            "created_at": pr.get("created_at"),
            "draft": pr.get("draft", False),
            "issue": linked,
            "issue_url": linked_row["url"] if linked_row else None,
            "lifecycle": linked_row["lifecycle"] if linked_row else None,
            "stats": pr_details.get(pr_n),
        })
    open_prs.sort(key=lambda p: p["created_at"] or "")

    # per-session work: effort signals that survive acus_consumed = 0
    session_work = []
    for sid, s in sessions.items():
        msgs = (session_msgs.get(sid) or {}).get("items", [])
        devin_n = sum(1 for m in msgs if m.get("source") == "devin")
        user_n = len(msgs) - devin_n
        created, updated = s.get("created_at"), s.get("updated_at")
        duration_h = round((updated - created) / 3600, 1) if created and updated else None
        pr_stats = [pr_details[p] for p in session_pr_numbers(s) if p in pr_details]
        session_work.append({
            "id": sid, "url": s.get("url"), "title": s.get("title") or "",
            "issue": session_issue.get(sid),
            "status": s.get("status"), "status_detail": s.get("status_detail"),
            "acus": s.get("acus_consumed") or 0,
            "duration_h": duration_h,
            "messages_devin": devin_n, "messages_user": user_n,
            "status_changes": max(len(session_history.get(sid, [])) - 1, 0),
            "additions": sum(p.get("additions") or 0 for p in pr_stats),
            "deletions": sum(p.get("deletions") or 0 for p in pr_stats),
            "changed_files": sum(p.get("changed_files") or 0 for p in pr_stats),
            "commits": sum(p.get("commits") or 0 for p in pr_stats),
        })
    session_work.sort(key=lambda w: (w["messages_devin"], w["additions"] + w["deletions"]), reverse=True)

    # sessions the pipeline can't tie to any issue
    unlinked = [
        {"id": sid, "url": s.get("url"), "status": s.get("status"),
         "title": s.get("title") or "", "acus": s.get("acus_consumed") or 0}
        for sid, s in sessions.items() if sid not in session_issue
    ]

    # --- cost ------------------------------------------------------------
    total_acus = sum(s.get("acus_consumed") or 0 for s in sessions.values())
    terminal = [s for s in sessions.values() if s.get("status") not in ("running", "suspended")]
    merged_pr_nums = {r["pr"]["number"] for r in merged}
    wasted_acus = sum(
        s.get("acus_consumed") or 0 for s in terminal
        if not any(p in merged_pr_nums for p in session_pr_numbers(s))
    )
    acu_list = sorted((s.get("acus_consumed") or 0) for s in sessions.values())
    cost = {
        "total_acus": round(total_acus, 2),
        "acu_usd": ACU_USD or None,
        "total_usd": round(total_acus * ACU_USD, 2) if ACU_USD else None,
        "usd_per_merge": round(total_acus * ACU_USD / len(merged), 2) if ACU_USD and merged else None,
        "acus_per_merge": round(total_acus / len(merged), 2) if merged else None,
        "wasted_acus": round(wasted_acus, 2),
        "acu_per_session": acu_list,
    }

    # --- value proxies -----------------------------------------------------
    lead_pr, lead_merge = [], []
    for r in rows:
        if r["pr"]:
            a, b = parse_dt(r["created_at"]), parse_dt(r["pr"]["created_at"])
            if a and b:
                lead_pr.append((b - a).total_seconds() / 3600)
            m = parse_dt(r["pr"]["merged_at"])
            if a and m:
                lead_merge.append((m - a).total_seconds() / 3600)
    reopened = {e["issue_number"] for e in events if e.get("event") == "reopened"}
    merged_stats = [pr_details.get(r["pr"]["number"]) for r in merged]
    merged_stats = [s for s in merged_stats if s]
    proxies = {
        "lines_merged_add": sum(s.get("additions") or 0 for s in merged_stats),
        "lines_merged_del": sum(s.get("deletions") or 0 for s in merged_stats),
        "devin_messages": sum(w["messages_devin"] for w in session_work),
        "merged": len(merged),
        "spawned": len(spawned),
        "survival_pct": round(100 * len(merged) / len(spawned), 1) if spawned else None,
        "closed_issues": sum(1 for r in rows if r["state"] == "closed"),
        "closed_by_devin": sum(1 for r in merged if r["state"] == "closed"),
        "auto_ok": sum(1 for r in rows if r["lifecycle"] == "devin-auto-ok"),
        "needs_review": sum(1 for r in rows if r["lifecycle"] == "devin-needs-review"),
        "median_hours_to_pr": median_hours(lead_pr),
        "median_hours_to_merge": median_hours(lead_merge),
        "reopened_after_merge": sum(1 for r in merged if r["number"] in reopened),
    }

    # --- trends ------------------------------------------------------------
    def bucket(iterable):
        out = {}
        for d in iterable:
            if d:
                out[d] = out.get(d, 0) + 1
        return out

    acu_burn = {}
    for sid, hist in session_history.items():
        prev = 0.0
        for fetched_at, payload in hist:
            cur = payload.get("acus_consumed") or 0.0
            delta = cur - prev
            if delta > 0:
                d = day_of(fetched_at)
                acu_burn[d] = round(acu_burn.get(d, 0) + delta, 2)
            prev = cur
    trends = {
        "issues_opened": bucket(day_of(r["created_at"]) for r in rows),
        "sessions_spawned": bucket(day_of(iso(s.get("created_at"))) for s in sessions.values()),
        "prs_merged": bucket(day_of(r["pr"]["merged_at"]) for r in merged),
        "acu_burn": acu_burn,
    }

    now = datetime.now(timezone.utc)
    last_ok = parse_dt(last_poll[1] or last_poll[0]) if last_poll else None
    return {
        "meta": {
            "repo": f"{os.environ.get('GH_TRACK_OWNER', 'tetratorus')}/{os.environ.get('GH_TRACK_REPO', 'superset')}",
            "generated_at": now.isoformat(),
            "last_poll": None if not last_poll else {
                "finished_at": last_poll[1], "ok": last_poll[2], "notes": last_poll[3],
            },
            "stale": bool(last_ok and (now - last_ok).total_seconds() > 2 * POLL_INTERVAL),
            "poll_interval": POLL_INTERVAL,
        },
        "funnel": funnel,
        "open_prs": open_prs,
        "session_work": session_work,
        "drift": drift,
        "unlinked_sessions": unlinked,
        "issues": rows,
        "cost": cost,
        "proxies": proxies,
        "trends": trends,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            try:
                body = json.dumps(build_state()).encode()
                ctype = "application/json"
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(BASE_DIR, "dashboard.html"), "rb") as f:
                body = f.read()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    print(f"Dashboard on http://localhost:{PORT} (db: {DB_PATH})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
