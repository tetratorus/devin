#!/usr/bin/env python3
"""Capture pipeline state (GitHub + Devin) into SQLite for the dashboard.

Read-only against GitHub and the Devin API; never writes to either. Appends
raw payloads to an observations table whenever an entity's payload changes,
so current state is the latest row per entity and history (ACU burn, status
changes, label churn) is the append trail. The DB is derived, disposable
state — deleting it only flattens the trend charts.

Usage:
    gh auth status                          # GitHub CLI authenticated
    export DEVIN_SERVICE_USER_KEY=cog_...
    export DEVIN_ORG_ID=...
    python3 collector.py           # loop every GH_TRACK_POLL_INTERVAL seconds
    python3 collector.py --once    # single cycle (for testing)
"""

import hashlib
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

OWNER = os.environ.get("GH_TRACK_OWNER", "tetratorus")
REPO = os.environ.get("GH_TRACK_REPO", "superset")
PER_PAGE = int(os.environ.get("GH_TRACK_PER_PAGE", "100"))
POLL_INTERVAL = int(os.environ.get("GH_TRACK_POLL_INTERVAL", "300"))
DB_PATH = os.environ.get("PIPELINE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.db"))

DEVIN_API_BASE = "https://api.devin.ai/v3"
DEVIN_API_KEY = os.environ.get("DEVIN_SERVICE_USER_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(kind, entity_key, id);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    notes TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def gh_api(endpoint: str) -> object | None:
    """GET via gh CLI; returns parsed JSON or None on failure."""
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh api {endpoint} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse gh api response for {endpoint}: {exc}", file=sys.stderr)
        return None


def devin_get(path: str) -> dict | None:
    req = urllib.request.Request(
        f"{DEVIN_API_BASE}{path}",
        headers={"Authorization": f"Bearer {DEVIN_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
            body = resp.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        print(f"Devin API GET {path} error {exc.code}: {exc.read().decode()}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"Devin API GET {path} failed: {exc}", file=sys.stderr)
    return None


def payload_hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def record(db: sqlite3.Connection, source: str, kind: str, entity_key: str, payload: object) -> bool:
    """Append an observation if the payload changed since the last one. Returns True if inserted."""
    h = payload_hash(payload)
    row = db.execute(
        "SELECT payload_hash FROM observations WHERE kind = ? AND entity_key = ? ORDER BY id DESC LIMIT 1",
        (kind, entity_key),
    ).fetchone()
    if row and row[0] == h:
        return False
    db.execute(
        "INSERT INTO observations (fetched_at, source, kind, entity_key, payload_hash, payload_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), source, kind, entity_key, h, json.dumps(payload, sort_keys=True)),
    )
    return True


def has_observation(db: sqlite3.Connection, kind: str, entity_key: str) -> bool:
    return db.execute(
        "SELECT 1 FROM observations WHERE kind = ? AND entity_key = ? LIMIT 1",
        (kind, entity_key),
    ).fetchone() is not None


def is_devin_issue(issue: dict) -> bool:
    """Issues the pipeline has touched: any devin-* label."""
    return any((lbl.get("name") or "").startswith("devin") for lbl in issue.get("labels", []))


def collect_once(db: sqlite3.Connection) -> str:
    counts = {"issue": 0, "pr": 0, "session": 0, "timeline_event": 0}
    errors = []

    # Issues (state=all: closed issues are where the value proxies live).
    # The Issues API includes PRs; those carry a pull_request key — skipped,
    # since the pulls endpoint captures them with richer fields.
    issues = gh_api(f"/repos/{OWNER}/{REPO}/issues?state=all&sort=updated&direction=desc&per_page={PER_PAGE}")
    changed_issue_numbers = []
    if issues is None:
        errors.append("issues fetch failed")
        issues = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        number = issue["number"]
        if record(db, "github", "issue", str(number), issue):
            counts["issue"] += 1
            if is_devin_issue(issue):
                changed_issue_numbers.append(number)

    # Timeline events, only for pipeline-touched issues whose payload moved
    # this cycle (events are immutable, so one fetch per change is enough).
    for number in changed_issue_numbers:
        events = gh_api(f"/repos/{OWNER}/{REPO}/issues/{number}/timeline?per_page={PER_PAGE}")
        if events is None:
            errors.append(f"timeline #{number} fetch failed")
            continue
        for event in events:
            event_id = event.get("id") or f"{event.get('event')}:{event.get('created_at')}"
            if record(db, "github", "timeline_event", f"{number}:{event_id}", {"issue_number": number, **event}):
                counts["timeline_event"] += 1

    # Pull requests, all states. Devin's are filtered at read time.
    # head.repo/base.repo are full repo objects with repo-wide counters
    # (open_issues, pushed_at) that tick on any repo activity — stripping
    # them keeps the change-dedup meaningful.
    prs = gh_api(f"/repos/{OWNER}/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}")
    if prs is None:
        errors.append("pulls fetch failed")
        prs = []
    for pr in prs:
        for side in ("head", "base"):
            repo = (pr.get(side) or {}).get("repo")
            if isinstance(repo, dict):
                pr[side]["repo"] = {"full_name": repo.get("full_name")}
        changed = record(db, "github", "pr", str(pr["number"]), pr)
        if changed:
            counts["pr"] += 1
        # diff stats (additions/deletions/files/commits) only exist on the
        # detail endpoint; fetch for Devin PRs when the PR changed or the
        # stats were never captured (backfill)
        is_devin = ((pr.get("user") or {}).get("login", "").endswith("[bot]")
                    or (pr.get("head") or {}).get("ref", "").startswith("devin/"))
        if is_devin:
            if changed or not has_observation(db, "pr_detail", str(pr["number"])):
                detail = gh_api(f"/repos/{OWNER}/{REPO}/pulls/{pr['number']}")
                if detail:
                    stats = {k: detail.get(k) for k in
                             ("number", "additions", "deletions", "changed_files", "commits")}
                    if record(db, "github", "pr_detail", str(pr["number"]), stats):
                        counts["pr_detail"] = counts.get("pr_detail", 0) + 1

    # Devin sessions. When a session changed, also refresh its message log —
    # message counts are the effort signal that survives acus_consumed = 0.
    data = devin_get(f"/organizations/{DEVIN_ORG_ID}/sessions?limit=100")
    if data is None:
        errors.append("sessions fetch failed")
    else:
        for session in data.get("items", []):
            sid = session.get("session_id")
            if not sid:
                continue
            changed = record(db, "devin", "session", sid, session)
            if changed:
                counts["session"] += 1
            if changed or not has_observation(db, "session_messages", sid):
                msgs = devin_get(f"/organizations/{DEVIN_ORG_ID}/sessions/{sid}/messages")
                if msgs is not None:
                    if record(db, "devin", "session_messages", sid,
                              {"session_id": sid, "total": msgs.get("total"),
                               "items": msgs.get("items", [])}):
                        counts["session_messages"] = counts.get("session_messages", 0) + 1

    changed_summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    notes = f"changed: {changed_summary or 'nothing'}"
    if errors:
        notes += f"; errors: {'; '.join(errors)}"
    return notes if not errors else f"ERR {notes}"


def poll(db: sqlite3.Connection) -> None:
    started = now_iso()
    cur = db.execute("INSERT INTO polls (started_at) VALUES (?)", (started,))
    poll_id = cur.lastrowid
    db.commit()
    try:
        notes = collect_once(db)
        ok = 0 if notes.startswith("ERR") else 1
    except Exception as exc:  # keep the loop alive; a poll row records the failure
        notes = f"ERR exception: {exc}"
        ok = 0
    db.execute(
        "UPDATE polls SET finished_at = ?, ok = ?, notes = ? WHERE id = ?",
        (now_iso(), ok, notes, poll_id),
    )
    db.commit()
    print(f"[{started}] poll {poll_id}: {notes}")


def main() -> None:
    if not DEVIN_API_KEY or not DEVIN_ORG_ID:
        print("DEVIN_SERVICE_USER_KEY and DEVIN_ORG_ID must be set.", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()

    once = "--once" in sys.argv
    print(f"Collecting {OWNER}/{REPO} -> {DB_PATH}" + ("" if once else f", every {POLL_INTERVAL}s"))
    while True:
        poll(db)
        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
