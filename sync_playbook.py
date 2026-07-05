#!/usr/bin/env python3
"""Sync playbook.md and knowledge/*.md to the Devin org as a playbook and knowledge notes.

Run this once before the tracker loop starts so Devin sessions receive the
latest playbook and knowledge. The script is idempotent: it creates resources
on first run and updates them by name on later runs.

Environment:
    DEVIN_SERVICE_USER_KEY    # Devin service user API key
    DEVIN_ORG_ID              # Devin organization ID

Output (when run as a script):
    JSON with playbook_id and knowledge_ids.
"""

import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

DEVIN_API_BASE = "https://api.devin.ai/v3"
API_KEY = os.environ.get("DEVIN_SERVICE_USER_KEY", "")
ORG_ID = os.environ.get("DEVIN_ORG_ID", "")

REPO_ROOT = Path(__file__).parent.resolve()
PLAYBOOK_FILE = REPO_ROOT / "playbook.md"
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"

PLAYBOOK_TITLE = "github-issue-tracker"


def devin_request(method: str, path: str, data: dict | None = None) -> dict | None:
    url = f"{DEVIN_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
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


def list_playbooks() -> list[dict]:
    data = devin_request("GET", f"/organizations/{ORG_ID}/playbooks")
    if data is None:
        return []
    return data.get("items", [])


def create_or_update_playbook() -> str:
    if not PLAYBOOK_FILE.exists():
        print(f"Playbook file not found: {PLAYBOOK_FILE}", file=sys.stderr)
        sys.exit(1)

    body = PLAYBOOK_FILE.read_text()
    existing = next((p for p in list_playbooks() if p.get("title") == PLAYBOOK_TITLE), None)

    if existing:
        playbook_id = existing["playbook_id"]
        print(f"Updating playbook {playbook_id}")
        devin_request(
            "PUT",
            f"/organizations/{ORG_ID}/playbooks/{playbook_id}",
            data={"title": PLAYBOOK_TITLE, "body": body},
        )
    else:
        print("Creating playbook")
        resp = devin_request(
            "POST",
            f"/organizations/{ORG_ID}/playbooks",
            data={"title": PLAYBOOK_TITLE, "body": body},
        )
        if resp is None:
            print("Failed to create playbook", file=sys.stderr)
            sys.exit(1)
        playbook_id = resp.get("playbook_id")

    return playbook_id


def list_knowledge_notes() -> list[dict]:
    data = devin_request("GET", f"/organizations/{ORG_ID}/knowledge/notes")
    if data is None:
        return []
    return data.get("items", [])


def create_or_update_knowledge_notes() -> list[str]:
    note_ids = []
    if not KNOWLEDGE_DIR.exists():
        return note_ids

    existing = {n.get("name"): n for n in list_knowledge_notes()}

    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        name = path.stem
        body = path.read_text()
        trigger = f"Guidance for {name.replace('_', ' ')}"

        if name in existing:
            note_id = existing[name]["note_id"]
            print(f"Updating knowledge note {note_id} ({name})")
            devin_request(
                "PUT",
                f"/organizations/{ORG_ID}/knowledge/notes/{note_id}",
                data={"name": name, "body": body, "trigger": trigger},
            )
        else:
            print(f"Creating knowledge note ({name})")
            resp = devin_request(
                "POST",
                f"/organizations/{ORG_ID}/knowledge/notes",
                data={"name": name, "body": body, "trigger": trigger},
            )
            if resp is None:
                print(f"Failed to create knowledge note {name}", file=sys.stderr)
                continue
            note_id = resp.get("note_id")

        if note_id:
            note_ids.append(note_id)

    return note_ids


def sync() -> dict:
    """Create or update the playbook and knowledge notes. Return their IDs."""
    if not API_KEY or not ORG_ID:
        print("DEVIN_SERVICE_USER_KEY and DEVIN_ORG_ID must be set.", file=sys.stderr)
        sys.exit(1)

    playbook_id = create_or_update_playbook()
    note_ids = create_or_update_knowledge_notes()
    return {"playbook_id": playbook_id, "knowledge_ids": note_ids}


if __name__ == "__main__":
    result = sync()
    print(json.dumps(result))
