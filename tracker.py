#!/usr/bin/env python3
"""GitHub issue tracker that hands new issues to Devin.

Polls the Issues API for a repository every POLL_INTERVAL seconds. For each
newly opened issue, spawns a Devin session (v3 API) to handle it. Keeps state
in a small file so each issue is only handed off once.

Requires:
    gh auth status                  # GitHub CLI authenticated
    export DEVIN_SERVICE_USER_KEY=cog_...   # Devin service user API key
    export DEVIN_ORG_ID=...                 # Settings > Service Users
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

# Configuration
OWNER = os.environ.get("GH_TRACK_OWNER", "tetratorus")
REPO = os.environ.get("GH_TRACK_REPO", "superset")
PER_PAGE = int(os.environ.get("GH_TRACK_PER_PAGE", "100"))
POLL_INTERVAL = int(os.environ.get("GH_TRACK_POLL_INTERVAL", "300"))
STATE_DIR = Path(os.environ.get("GH_TRACK_STATE_DIR", Path.home() / ".local" / "share" / "github-event-tracker"))
STATE_FILE = STATE_DIR / f"{OWNER}-{REPO}-last-issue-number"

DEVIN_API_BASE = "https://api.devin.ai/v3"
DEVIN_API_KEY = os.environ.get("DEVIN_SERVICE_USER_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")


def run_gh_api(endpoint: str):
    """Call gh api and return the parsed JSON response."""
    cmd = ["gh", "api", endpoint]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh api failed: {result.stderr}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse gh api response: {exc}", file=sys.stderr)
        return None


def load_last_issue_number() -> int:
    if not STATE_FILE.exists():
        return -1
    try:
        return int(STATE_FILE.read_text().strip())
    except ValueError:
        return -1


def save_last_issue_number(number: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(number))


def fetch_issues() -> list[dict] | None:
    """Fetch open issues, newest first. Excludes pull requests."""
    endpoint = f"/repos/{OWNER}/{REPO}/issues?state=open&sort=created&direction=desc&per_page={PER_PAGE}"
    issues = run_gh_api(endpoint)
    if issues is None:
        return None
    # The Issues API also returns PRs; they carry a "pull_request" key.
    return [i for i in issues if "pull_request" not in i]


def spawn_devin_session(issue: dict) -> str | None:
    """Create a Devin session for the issue. Returns the session URL."""
    number = issue["number"]
    title = issue.get("title", "")
    body = issue.get("body") or "(no description)"
    url = issue.get("html_url", "")

    prompt = (
        f"Repo: {OWNER}/{REPO}\n"
        f"GitHub issue #{number}: {title}\n"
        f"{url}\n\n"
        f"Issue description:\n{body}\n\n"
        f"Investigate and resolve this issue, then open a pull request "
        f"against {OWNER}/{REPO} that references issue #{number}."
    )

    req = urllib.request.Request(
        f"{DEVIN_API_BASE}/organizations/{DEVIN_ORG_ID}/sessions",
        data=json.dumps({
            "prompt": prompt,
            "title": f"{OWNER}/{REPO} issue #{number}: {title}",
        }).encode(),
        headers={
            "Authorization": f"Bearer {DEVIN_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
            session = json.loads(resp.read())
        return session.get("url")
    except urllib.error.HTTPError as exc:
        print(f"Devin API error {exc.code} for issue #{number}: {exc.read().decode()}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"Devin API request failed for issue #{number}: {exc}", file=sys.stderr)
    return None


def poll_once(last_number: int) -> int:
    """Run one poll pass. Returns the updated last-seen issue number."""
    issues = fetch_issues()
    if issues is None:
        return last_number  # transient failure; retry next cycle

    newest = max((i["number"] for i in issues), default=0)

    if last_number < 0:
        # First run: baseline only, don't hand existing issues to Devin.
        save_last_issue_number(newest)
        print(f"Initialized baseline at issue #{newest} for {OWNER}/{REPO}. "
              f"Only issues opened after this will be handed to Devin.")
        return newest

    new_issues = sorted((i for i in issues if i["number"] > last_number), key=lambda i: i["number"])

    if not new_issues:
        print(f"No new issues for {OWNER}/{REPO} (last seen: #{last_number}).")
        return last_number

    for issue in new_issues:
        number = issue["number"]
        print(f"[NEW ISSUE] #{number}: {issue.get('title', '?')}\n  {issue.get('html_url', '')}")
        session_url = spawn_devin_session(issue)
        if session_url:
            print(f"  -> Devin session: {session_url}")
            last_number = number
            save_last_issue_number(last_number)
        else:
            # Don't advance past a failed handoff; retry it next cycle.
            print(f"  -> Failed to spawn Devin session for #{number}; will retry next poll.", file=sys.stderr)
            break

    return last_number


def main() -> None:
    if not DEVIN_API_KEY or not DEVIN_ORG_ID:
        print("DEVIN_SERVICE_USER_KEY and DEVIN_ORG_ID must be set.", file=sys.stderr)
        sys.exit(1)

    last_number = load_last_issue_number()
    print(f"Tracking {OWNER}/{REPO}, polling every {POLL_INTERVAL}s.")
    while True:
        last_number = poll_once(last_number)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
