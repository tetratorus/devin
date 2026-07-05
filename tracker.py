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
from datetime import datetime, timezone
from pathlib import Path

import sync_playbook

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

_PLAYBOOK_ID = None
_KNOWLEDGE_IDS = None

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "pr_url": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
        "review_points": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["pr_url", "needs_human_review"],
}


def load_playbook() -> tuple[str, list[str]]:
    """Sync the playbook and knowledge notes once, caching their IDs."""
    global _PLAYBOOK_ID, _KNOWLEDGE_IDS
    if _PLAYBOOK_ID is None:
        result = sync_playbook.sync()
        _PLAYBOOK_ID = result.get("playbook_id")
        _KNOWLEDGE_IDS = result.get("knowledge_ids", [])
    return _PLAYBOOK_ID, _KNOWLEDGE_IDS


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


def github_timestamp(ts: str) -> int:
    """Parse a GitHub ISO 8601 timestamp into Unix seconds."""
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0


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
TERMINAL_STATUSES = {"error", "exited", "exit", "terminated", "failed"}
SUSPENDED_TERMINAL_DETAILS = {"out_of_credits", "usage_limit_exceeded"}
LIFECYCLE_LABELS = {
    "devin",
    "devin-pr",
    "devin-needs-review",
    "devin-auto-ok",
    "devin-error",
    "devin-abandoned",
    "devin-hold",
}

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


def is_terminal_session(session: dict) -> bool:
    """Return True if the session is in a terminal state."""
    status = (session.get("status") or "").lower()
    detail = (session.get("status_detail") or "").lower()
    if status in TERMINAL_STATUSES:
        return True
    if status == "suspended" and detail in SUSPENDED_TERMINAL_DETAILS:
        return True
    return False


def get_session_for_lifecycle(number: int) -> dict | None:
    """Return the best session to inspect for lifecycle decisions.

    Prefers the newest live session; falls back to the newest session overall.
    """
    live = get_live_session_for_issue(number)
    if live:
        return live
    sessions = get_sessions_for_issue(number)
    return sessions[0] if sessions else None


def get_pr_state(pr_url: str) -> str | None:
    """Return the GitHub state of a PR: 'open', 'closed', or 'merged'."""
    try:
        pr_number = int(pr_url.split("/")[-1])
    except (ValueError, IndexError):
        return None
    pr = gh_api(f"/repos/{OWNER}/{REPO}/pulls/{pr_number}")
    if pr is None:
        return None
    if pr.get("merged"):
        return "merged"
    return pr.get("state")


def get_review_label(session: dict) -> str:
    """Map the session's structured output to the review lifecycle label."""
    output = session.get("structured_output") or {}
    if output.get("needs_human_review"):
        return "devin-needs-review"
    return "devin-auto-ok"


def advance_lifecycle_label(issue: dict, session: dict | None) -> str | None:
    """Decide the next lifecycle label for a tracked issue, or None if no change."""
    labels = {l.get("name") for l in issue.get("labels", [])}
    current = labels & LIFECYCLE_LABELS

    if "devin" in current:
        if session is None:
            return None
        if is_terminal_session(session):
            return "devin-error"
        prs = session.get("pull_requests", [])
        if prs:
            states = [get_pr_state(pr.get("pr_url", "")) for pr in prs]
            if any(s == "open" for s in states):
                return "devin-pr"
            if any(s == "merged" for s in states):
                return get_review_label(session)
            if any(s == "closed" for s in states):
                return "devin-error"
        return None

    if "devin-pr" in current:
        if session is None:
            return "devin-error"
        prs = session.get("pull_requests", [])
        if not prs:
            return "devin-error"
        states = [get_pr_state(pr.get("pr_url", "")) for pr in prs]
        if any(s == "open" for s in states):
            return None
        if any(s == "merged" for s in states):
            return get_review_label(session)
        return "devin-error"

    return None


def set_lifecycle_label(issue: dict, new_label: str) -> None:
    """Replace the issue's lifecycle labels with new_label."""
    number = issue["number"]
    current_labels = {l.get("name") for l in issue.get("labels", [])}
    for label in current_labels & LIFECYCLE_LABELS:
        if label != new_label:
            remove_label(number, label)
    if new_label not in current_labels:
        add_label(number, new_label)
        print(f"  -> Lifecycle label on issue #{number}: {new_label}")


def terminate_session(session_id: str) -> bool:
    """Terminate a live Devin session."""
    return devin_request(
        "DELETE",
        f"/organizations/{DEVIN_ORG_ID}/sessions/{session_id}",
    ) is not None


def get_pr(pr_url: str) -> dict | None:
    """Fetch a PR object from its URL."""
    try:
        pr_number = int(pr_url.split("/")[-1])
    except (ValueError, IndexError):
        return None
    return gh_api(f"/repos/{OWNER}/{REPO}/pulls/{pr_number}")


def close_pr_and_delete_branch(pr_url: str) -> bool:
    """Close a PR and delete its head branch."""
    pr_number = int(pr_url.split("/")[-1])
    pr = get_pr(pr_url)
    if pr is None:
        return False
    branch = pr.get("head", {}).get("ref")
    closed = gh_api(
        f"/repos/{OWNER}/{REPO}/pulls/{pr_number}",
        method="PATCH",
        fields={"state": "closed"},
    ) is not None
    if closed and branch:
        gh_api(f"/repos/{OWNER}/{REPO}/git/refs/heads/{branch}", method="DELETE")
    return closed


def has_ci_failure_comment(number: int, sha: str, tracker_user: str) -> bool:
    """Return True if the tracker already posted a CI-failure comment for this SHA."""
    marker = f"<!-- devin-ci-failed: {sha} -->"
    comments = fetch_comments(number)
    return any(
        c.get("user", {}).get("login") == tracker_user and marker in (c.get("body") or "")
        for c in comments
    )


def post_ci_failure_comment(number: int, sha: str, failed_names: list[str]) -> None:
    """Post a CI failure notice on the issue, idempotent by SHA."""
    body = (
        f"CI failed on commit `{sha}` for the failing checks: "
        f"{', '.join(failed_names)}.\n\n"
        f"<!-- devin-ci-failed: {sha} -->"
    )
    post_comment(number, body)


def handle_ci_feedback(issue: dict, session: dict | None, tracker_user: str) -> None:
    """Check PR check runs and surface failures as a label + optional session message."""
    number = issue["number"]
    prs = session.get("pull_requests", []) if session else []
    open_pr_url = next((pr.get("pr_url") for pr in prs if get_pr_state(pr.get("pr_url", "")) == "open"), None)
    if open_pr_url is None:
        return

    pr = get_pr(open_pr_url)
    if pr is None:
        return
    head_sha = pr.get("head", {}).get("sha")
    if not head_sha:
        return

    check_data = gh_api(f"/repos/{OWNER}/{REPO}/commits/{head_sha}/check-runs")
    if check_data is None:
        return

    runs = [r for r in check_data.get("check_runs", []) if r.get("status") == "completed"]
    failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out")]

    if failed:
        failed_names = [r.get("name", "unknown") for r in failed]
        if not has_ci_failure_comment(number, head_sha, tracker_user):
            post_ci_failure_comment(number, head_sha, failed_names)
            print(f"  -> CI failed on issue #{number}: {', '.join(failed_names)}")
        if not issue_has_label(issue, "devin-ci-failed"):
            add_label(number, "devin-ci-failed")
        if session and session.get("status", "").lower() in LIVE_STATUSES:
            send_session_message(
                session.get("session_id"),
                f"CI failed on commit {head_sha}: {', '.join(failed_names)}. Please push a fix.",
            )
    else:
        if issue_has_label(issue, "devin-ci-failed"):
            remove_label(number, "devin-ci-failed")
            print(f"  -> CI green on issue #{number}; removed devin-ci-failed.")


def handle_closed_issues() -> None:
    """Clean up live sessions and open PRs for issues that were closed mid-flight."""
    endpoint = f"/repos/{OWNER}/{REPO}/issues?state=closed&sort=updated&direction=desc&per_page={PER_PAGE}"
    issues = gh_api(endpoint)
    if issues is None:
        return

    for issue in issues:
        if "pull_request" in issue:
            continue
        number = issue["number"]
        labels = {l.get("name") for l in issue.get("labels", [])}
        lifecycle = labels & LIFECYCLE_LABELS
        if not lifecycle:
            continue
        if "devin-abandoned" in labels or "devin-error" in labels:
            continue

        session = get_session_for_lifecycle(number)
        prs = session.get("pull_requests", []) if session else []

        merged_prs = []
        open_prs = []
        for pr_info in prs:
            pr_url = pr_info.get("pr_url", "")
            state = get_pr_state(pr_url)
            if state == "merged":
                merged_prs.append(pr_url)
            elif state == "open":
                open_prs.append(pr_url)

        if merged_prs:
            # A merged PR cannot be safely auto-reverted; surface the revert command for a human.
            for pr_url in merged_prs:
                pr = get_pr(pr_url)
                if pr is None:
                    continue
                merge_sha = pr.get("merge_commit_sha")
                if merge_sha:
                    comment = (
                        f"The associated PR was merged before this issue was closed. "
                        f"If you need to undo it, run: `git revert -m 1 {merge_sha}`"
                    )
                    post_comment(number, comment)
            if not (labels & {"devin-needs-review", "devin-auto-ok"}):
                set_lifecycle_label(issue, "devin-needs-review")
            continue

        if session and session.get("status", "").lower() in LIVE_STATUSES:
            if terminate_session(session.get("session_id")):
                print(f"  -> Terminated session for closed issue #{number}.")

        for pr_url in open_prs:
            close_pr_and_delete_branch(pr_url)
            print(f"  -> Closed PR and deleted branch for closed issue #{number}.")

        set_lifecycle_label(issue, "devin-abandoned")


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


def relay_comments(issue: dict, session: dict, tracker_user: str) -> None:
    """Relay new trusted comments on a tracked issue to its live Devin session."""
    number = issue["number"]
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


WAITING_DETAILS = {"waiting_for_user", "waiting_for_approval"}


def check_blocked_states(issue: dict, session: dict, tracker_user: str) -> None:
    """Nudge Devin to post on the issue when waiting, and surface suspensions."""
    number = issue["number"]
    session_id = session.get("session_id")
    if not session_id:
        return

    status = session.get("status", "").lower()
    status_detail = session.get("status_detail", "").lower()
    updated_at = session.get("updated_at", 0)

    if status_detail in WAITING_DETAILS:
        # Only nudge if the session has been waiting long enough to avoid spam.
        if time.time() - updated_at < 60:
            return

        comments = fetch_comments(number)
        bot_comments = [c for c in comments if is_bot_comment(c)]
        latest_bot = max(bot_comments, key=lambda c: github_timestamp(c.get("created_at", ""))) if bot_comments else None
        if latest_bot and github_timestamp(latest_bot.get("created_at", "")) > updated_at:
            return

        if send_session_message(session_id, "Please post your question or status update to the GitHub issue thread."):
            print(f"  -> Nudged session {session_id[:10]} to post on issue #{number}.")
        else:
            print(f"  -> Failed to nudge session {session_id[:10]} on issue #{number}.", file=sys.stderr)

    elif status == "suspended":
        reason = session.get("status_detail", "unknown")
        notice = f"session suspended: {reason}"
        comments = fetch_comments(number)
        tracker_comments = [c for c in comments if (c.get("user") or {}).get("login") == tracker_user]
        if any(c.get("body") == notice for c in tracker_comments):
            return

        if post_comment(number, notice):
            print(f"  -> Posted suspension notice for issue #{number}.")
        else:
            print(f"  -> Failed to post suspension notice for issue #{number}.", file=sys.stderr)


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

    playbook_id, knowledge_ids = load_playbook()

    session = devin_request(
        "POST",
        f"/organizations/{DEVIN_ORG_ID}/sessions",
        data={
            "prompt": prompt,
            "title": f"{OWNER}/{REPO} issue #{number}: {title}",
            "tags": [f"issue:{number}"],
            "playbook_id": playbook_id,
            "knowledge_ids": knowledge_ids,
            "structured_output_required": True,
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        },
    )
    if session is None:
        return None
    return session.get("url")


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["auto", "review", "hold"]},
        "rule_fired": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["decision", "rule_fired", "reason", "confidence"],
}

TRIAGE_RUBRIC = Path(__file__).parent / "knowledge" / "rubrics" / "triage.md"
TRIAGE_POLL_SECONDS = 15
TRIAGE_TIMEOUT_SECONDS = 300


def triage_issue(number: int, title: str, body: str) -> dict | None:
    """Classify an issue against the triage rubric via a classify-only Devin
    session. Returns {decision, rule_fired, reason, confidence} or None on
    failure/timeout. The session is terminated once the verdict is read."""
    if not TRIAGE_RUBRIC.exists():
        print(f"Triage rubric missing at {TRIAGE_RUBRIC}; skipping triage.", file=sys.stderr)
        return {"decision": "auto", "rule_fired": "none", "reason": "rubric missing", "confidence": "low"}

    prompt = (
        "You are the TRIAGE component of an issue-to-agent pipeline.\n"
        "Your ONLY job is to classify ONE issue. Do NOT clone any repository, do NOT\n"
        "write code, do NOT open PRs, do NOT comment on GitHub. Read, classify,\n"
        "return structured output, done.\n\n"
        f"Rubric:\n\n{TRIAGE_RUBRIC.read_text()}\n\n"
        f"Issue #{number}: {title}\n\n{body}\n\n"
        "Return your classification via structured output."
    )
    session = devin_request(
        "POST",
        f"/organizations/{DEVIN_ORG_ID}/sessions",
        data={
            "prompt": prompt,
            "title": f"TRIAGE issue #{number}: {title[:80]}",
            "tags": [f"triage:{number}", "triage"],
            "unlisted": True,
            "structured_output_required": True,
            "structured_output_schema": TRIAGE_SCHEMA,
        },
    )
    if session is None:
        return None
    session_id = session.get("session_id")

    verdict = None
    deadline = time.time() + TRIAGE_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(TRIAGE_POLL_SECONDS)
        state = devin_request("GET", f"/organizations/{DEVIN_ORG_ID}/sessions/{session_id}")
        if state is None:
            continue
        out = state.get("structured_output")
        if out and out.get("decision"):
            verdict = out
            break
        if state.get("status") in ("exit", "error"):
            break
    terminate_session(session_id)
    return verdict


def poll_once() -> None:
    """Run one poll pass. Hand off any open issue not yet tracked on GitHub."""
    handle_closed_issues()

    issues = fetch_issues()
    if issues is None:
        return  # transient failure; retry next cycle

    tracker_user = get_tracker_user()

    for issue in sorted(issues, key=lambda i: i["number"]):
        number = issue["number"]

        if issue_has_label(issue, "devin") or issue_has_label(issue, "devin-pr"):
            session = get_session_for_lifecycle(number)
            if session and session.get("status", "").lower() in LIVE_STATUSES:
                relay_comments(issue, session, tracker_user)
                check_blocked_states(issue, session, tracker_user)
            else:
                status = session.get("status", "none") if session else "none"
                print(f"[TRACKED] Issue #{number}: no live session (status: {status}).")

            if issue_has_label(issue, "devin-pr"):
                handle_ci_feedback(issue, session, tracker_user)

            new_label = advance_lifecycle_label(issue, session)
            if new_label:
                set_lifecycle_label(issue, new_label)
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

        print(f"  -> Triaging issue #{number} against the rubric...")
        verdict = triage_issue(number, title, body)
        if verdict is None:
            print(f"  -> Triage failed for issue #{number}; removing lock and retrying next poll.", file=sys.stderr)
            remove_label(number, "devin")
            continue

        decision = verdict.get("decision")
        summary = f"**Triage: {decision}** ({verdict.get('rule_fired')}, confidence {verdict.get('confidence')}) — {verdict.get('reason')}"
        print(f"  -> Triage verdict for #{number}: {decision} ({verdict.get('confidence')})")

        if decision == "hold" or verdict.get("confidence") == "low":
            remove_label(number, "devin")
            add_label(number, "devin-hold")
            held = "held for a human — no Devin session was spawned" if decision == "hold" \
                else "low-confidence classification, treated as hold — no Devin session was spawned"
            comment = post_comment(number, f"{summary}\n\nThis issue was {held}. "
                                   "A trusted user can remove the `devin-hold` label to re-triage.")
            if comment:
                mark_relayed_comment(comment.get("id"))
            continue

        session_url = spawn_devin_session(issue, body)
        if session_url:
            print(f"  -> Devin session: {session_url}")
            comment = post_comment(number, f"{summary}\n\nDevin session started: {session_url}")
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

    for label in LIFECYCLE_LABELS:
        if not ensure_label(label):
            print(f"Failed to ensure the '{label}' label exists in the repo.", file=sys.stderr)
            sys.exit(1)

    if not ensure_label("devin-ci-failed"):
        print("Failed to ensure the 'devin-ci-failed' label exists in the repo.", file=sys.stderr)
        sys.exit(1)

    load_playbook()

    print(f"Tracking {OWNER}/{REPO}, polling every {POLL_INTERVAL}s.")
    while True:
        poll_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
