#!/usr/bin/env python3
"""GitHub issue tracker that hands new issues to Devin.

Polls the Issues API for a repository every POLL_INTERVAL seconds. For each
open issue that is not already tracked on GitHub, spawns a Devin session (v3
API). All pipeline state lives on GitHub (labels + comments), so the tracker is
stateless and safe to run in parallel.

Requires:
    gh auth status                  # GitHub CLI authenticated (or GH_TOKEN)
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

DEVIN_API_BASE = "https://api.devin.ai/v3"
DEVIN_API_KEY = os.environ.get("DEVIN_SERVICE_USER_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def gh_api(endpoint: str, method: str = "GET", fields: dict | None = None):
    """Call gh api and return the parsed JSON response (or None on failure)."""
    cmd = ["gh", "api", "--method", method, endpoint]
    if fields:
        for key, value in fields.items():
            cmd.extend(["--field", f"{key}={value}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh api {method} {endpoint} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse gh api response: {exc}", file=sys.stderr)
        return None


def ensure_label(label: str) -> bool:
    """Ensure a GitHub label exists, creating it if necessary."""
    existing = gh_api(f"/repos/{OWNER}/{REPO}/labels/{label}")
    if existing is not None:
        return True
    color = "0e8a16" if label == "devin" else "ffffff"
    created = gh_api(
        f"/repos/{OWNER}/{REPO}/labels",
        method="POST",
        fields={"name": label, "color": color, "description": f"Managed by Devin tracker ({label})"},
    )
    return created is not None


def add_label(number: int, label: str) -> bool:
    return gh_api(
        f"/repos/{OWNER}/{REPO}/issues/{number}/labels",
        method="POST",
        fields={"labels[]": label},
    ) is not None


def remove_label(number: int, label: str) -> bool:
    return gh_api(
        f"/repos/{OWNER}/{REPO}/issues/{number}/labels/{label}",
        method="DELETE",
    ) is not None


def post_comment(number: int, body: str) -> dict | None:
    return gh_api(
        f"/repos/{OWNER}/{REPO}/issues/{number}/comments",
        method="POST",
        fields={"body": body},
    )


def issue_has_label(issue: dict, label: str) -> bool:
    return any(lbl.get("name") == label for lbl in issue.get("labels", []))


def is_trusted(author_association: str) -> bool:
    return author_association.upper() in TRUSTED_ASSOCIATIONS


def is_approval_comment(body: str) -> bool:
    """A trusted comment that explicitly approves using the original issue text."""
    text = body.lower()
    return any(marker in text for marker in ("approve", "approved", "lgtm"))


def fetch_comments(number: int) -> list[dict]:
    comments = gh_api(f"/repos/{OWNER}/{REPO}/issues/{number}/comments")
    if comments is None:
        return []
    return comments


def get_issue_prompt_source(issue: dict) -> tuple[str, str] | None:
    """
    Return the (title, body) to use as the Devin prompt source for this issue.

    - Trusted author: use the original title/body.
    - Untrusted author: require a trusted comment. If it is an explicit approval,
      use the original title/body; otherwise use the latest trusted comment as
      a restatement. If no trusted comment is present, return None so the issue
      is placed on hold.
    """
    number = issue["number"]
    title = issue.get("title", "")
    body = issue.get("body") or "(no description)"
    author_association = issue.get("author_association", "").upper()

    if is_trusted(author_association):
        return (title, body)

    comments = fetch_comments(number)
    trusted_comments = [c for c in comments if is_trusted(c.get("author_association", "").upper())]
    if not trusted_comments:
        return None

    latest = trusted_comments[-1]
    if is_approval_comment(latest.get("body", "")):
        return (title, body)

    restatement = latest.get("body") or "(no description)"
    restatement_title = f"Restatement from {latest.get('user', {}).get('login', 'trusted user')} on issue #{number}"
    return (restatement_title, restatement)


def devin_request(method: str, path: str, data: dict | None = None) -> dict | None:
    """Make a Devin v3 API request and return the parsed JSON response."""
    url = f"{DEVIN_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {DEVIN_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    if data is not None:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
            body = resp.read()
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        print(f"Devin API {method} {path} error {exc.code}: {exc.read().decode()}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"Devin API {method} {path} failed: {exc}", file=sys.stderr)
    return None


def find_session_by_issue_tag(number: int) -> bool:
    """Return True if any Devin session is tagged for this issue.

    Filters client-side: the organization list endpoint accepts the `tags`
    query param, but the server-side filter does not reliably narrow by tag,
    so we fetch the page and inspect the tags on each session.
    """
    tag = f"issue:{number}"
    data = devin_request("GET", f"/organizations/{DEVIN_ORG_ID}/sessions?limit=100")
    if data is None:
        return False
    return any(tag in session.get("tags", []) for session in data.get("items", []))


LIVE_STATUSES = {"running", "suspended", "waiting_for_user", "waiting_for_approval"}

_TRACKER_USER = None


def get_tracker_user() -> str:
    """Return the login of the authenticated GitHub user (cached)."""
    global _TRACKER_USER
    if _TRACKER_USER is None:
        user = gh_api("/user")
        _TRACKER_USER = user.get("login", "") if user else ""
    return _TRACKER_USER


def is_bot_comment(comment: dict) -> bool:
    """Return True for comments from Devin or other bot users."""
    login = (comment.get("user") or {}).get("login", "")
    user_type = (comment.get("user") or {}).get("type", "")
    return user_type == "Bot" or login.endswith("[bot]")


def mark_relayed_comment(comment_id: int) -> bool:
    """Mark a comment as already relayed by adding an eyes reaction."""
    return gh_api(
        f"/repos/{OWNER}/{REPO}/issues/comments/{comment_id}/reactions",
        method="POST",
        fields={"content": "eyes"},
    ) is not None


def is_relayed_comment(comment_id: int, tracker_user: str) -> bool:
    """Return True if the tracker already has an eyes reaction on this comment."""
    reactions = gh_api(f"/repos/{OWNER}/{REPO}/issues/comments/{comment_id}/reactions")
    if reactions is None:
        return False
    return any(
        r.get("content") == "eyes" and (r.get("user") or {}).get("login") == tracker_user
        for r in reactions
    )


def get_sessions_for_issue(number: int) -> list[dict]:
    """Return all Devin sessions tagged for this issue, newest first."""
    tag = f"issue:{number}"
    data = devin_request("GET", f"/organizations/{DEVIN_ORG_ID}/sessions?limit=100")
    if data is None:
        return []
    sessions = [s for s in data.get("items", []) if tag in s.get("tags", [])]
    sessions.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return sessions


def get_live_session_for_issue(number: int) -> dict | None:
    """Return the newest live session for this issue, or None if all are terminal."""
    for session in get_sessions_for_issue(number):
        if session.get("status", "").lower() in LIVE_STATUSES:
            return session
    return None


def send_session_message(session_id: str, message: str) -> bool:
    """Send a message to a live Devin session."""
    return devin_request(
        "POST",
        f"/organizations/{DEVIN_ORG_ID}/sessions/{session_id}/messages",
        data={"message": message},
    ) is not None


def relay_comments(issue: dict, tracker_user: str) -> None:
    """Relay new trusted comments on a tracked issue to its live Devin session."""
    number = issue["number"]
    session = get_live_session_for_issue(number)
    if session is None:
        sessions = get_sessions_for_issue(number)
        status = sessions[0].get("status", "none") if sessions else "none"
        print(f"[COMMENTS] Issue #{number}: no live session (status: {status}).")
        return

    session_id = session.get("session_id")
    if not session_id:
        return

    comments = fetch_comments(number)
    if not comments:
        print(f"[COMMENTS] Issue #{number}: no comments.")
        return

    relayed = 0
    for comment in comments:
        if is_bot_comment(comment):
            continue
        if not is_trusted(comment.get("author_association", "").upper()):
            continue
        comment_id = comment.get("id")
        if comment_id is None:
            continue
        if is_relayed_comment(comment_id, tracker_user):
            continue

        login = (comment.get("user") or {}).get("login", "unknown")
        body = comment.get("body") or ""
        message = f"New comment from {login} on issue #{number}:\n\n{body}"
        if send_session_message(session_id, message):
            mark_relayed_comment(comment_id)
            relayed += 1
            print(f"  -> Relayed comment #{comment_id} from {login} to session {session_id[:10]}.")
        else:
            print(f"  -> Failed to relay comment #{comment_id} from {login}.", file=sys.stderr)

    if relayed == 0:
        print(f"[COMMENTS] Issue #{number}: no new trusted comments to relay.")
    else:
        print(f"[COMMENTS] Issue #{number}: relayed {relayed} comment(s).")


def fetch_issues() -> list[dict] | None:
    """Fetch open issues, newest first. Excludes pull requests."""
    endpoint = f"/repos/{OWNER}/{REPO}/issues?state=open&sort=created&direction=desc&per_page={PER_PAGE}"
    issues = gh_api(endpoint)
    if issues is None:
        return None
    # The Issues API also returns PRs; they carry a "pull_request" key.
    return [i for i in issues if "pull_request" not in i]


def spawn_devin_session(issue: dict, source_body: str) -> str | None:
    """Create a Devin session for the issue. Returns the session URL."""
    number = issue["number"]
    title = issue.get("title", "")
    url = issue.get("html_url", "")

    prompt = (
        f"Repo: {OWNER}/{REPO}\n"
        f"GitHub issue #{number}: {title}\n"
        f"{url}\n\n"
        f"Issue description:\n{source_body}\n\n"
        f"Investigate and resolve this issue, then open a pull request "
        f"against {OWNER}/{REPO} that references issue #{number}."
    )

    session = devin_request(
        "POST",
        f"/organizations/{DEVIN_ORG_ID}/sessions",
        data={
            "prompt": prompt,
            "title": f"{OWNER}/{REPO} issue #{number}: {title}",
            "tags": [f"issue:{number}"],
        },
    )
    if session is None:
        return None
    return session.get("url")


def poll_once() -> None:
    """Run one poll pass. Hand off any open issue not yet tracked on GitHub."""
    issues = fetch_issues()
    if issues is None:
        return  # transient failure; retry next cycle

    tracker_user = get_tracker_user()

    for issue in sorted(issues, key=lambda i: i["number"]):
        number = issue["number"]

        if issue_has_label(issue, "devin"):
            relay_comments(issue, tracker_user)
            continue

        if issue_has_label(issue, "devin-hold"):
            print(f"Skipping issue #{number}: on hold (devin-hold).")
            continue

        prompt_source = get_issue_prompt_source(issue)
        if prompt_source is None:
            print(f"[HOLD] Issue #{number} is untrusted and has no trusted approval or restatement.")
            add_label(number, "devin-hold")
            continue

        if find_session_by_issue_tag(number):
            print(f"Skipping issue #{number}: existing Devin session tagged.")
            continue

        title, body = prompt_source
        print(f"[NEW ISSUE] #{number}: {title}\n  {issue.get('html_url', '')}")

        if not add_label(number, "devin"):
            print(f"  -> Failed to lock devin label on issue #{number}; will retry next poll.", file=sys.stderr)
            continue

        if find_session_by_issue_tag(number):
            # Another tracker raced us and created a session after we added the label.
            print(f"  -> Issue #{number} already has a session after lock; will skip.")
            continue

        session_url = spawn_devin_session(issue, body)
        if session_url:
            print(f"  -> Devin session: {session_url}")
            comment = post_comment(number, f"Devin session started: {session_url}")
            if comment:
                mark_relayed_comment(comment.get("id"))
            else:
                print(f"  -> Failed to comment on issue #{number}.", file=sys.stderr)
        else:
            print(f"  -> Failed to spawn Devin session for #{number}; removing lock and retrying next poll.", file=sys.stderr)
            remove_label(number, "devin")


def main() -> None:
    if not DEVIN_API_KEY or not DEVIN_ORG_ID:
        print("DEVIN_SERVICE_USER_KEY and DEVIN_ORG_ID must be set.", file=sys.stderr)
        sys.exit(1)

    if not ensure_label("devin"):
        print("Failed to ensure the 'devin' label exists in the repo.", file=sys.stderr)
        sys.exit(1)

    if not ensure_label("devin-hold"):
        print("Failed to ensure the 'devin-hold' label exists in the repo.", file=sys.stderr)
        sys.exit(1)

    print(f"Tracking {OWNER}/{REPO}, polling every {POLL_INTERVAL}s.")
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
