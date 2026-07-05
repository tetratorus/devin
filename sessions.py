#!/usr/bin/env python3
"""Print a table of Devin sessions in the org.

Usage:
    export DEVIN_SERVICE_USER_KEY=cog_...
    export DEVIN_ORG_ID=...
    python3 sessions.py
"""

import json
import os
import ssl
import sys
import urllib.request
from collections import Counter
from datetime import datetime

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

API_KEY = os.environ.get("DEVIN_SERVICE_USER_KEY", "")
ORG_ID = os.environ.get("DEVIN_ORG_ID", "")

if not API_KEY or not ORG_ID:
    print("DEVIN_SERVICE_USER_KEY and DEVIN_ORG_ID must be set.", file=sys.stderr)
    sys.exit(1)

req = urllib.request.Request(
    f"https://api.devin.ai/v3/organizations/{ORG_ID}/sessions?limit=100",
    headers={"Authorization": f"Bearer {API_KEY}"},
)
with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp:
    data = json.loads(resp.read())

sessions = data.get("items", [])
sessions.sort(key=lambda s: s.get("created_at", 0), reverse=True)

fmt = "{:<10} {:<12} {:>6} {:<17} {:<5} {:<50}"
print(fmt.format("SESSION", "STATUS", "ACUS", "CREATED", "PRS", "TITLE"))
for s in sessions:
    created = datetime.fromtimestamp(s.get("created_at", 0)).strftime("%m-%d %H:%M")
    title = (s.get("title") or "")[:50]
    print(fmt.format(
        s["session_id"][:10],
        s.get("status", "?"),
        f"{s.get('acus_consumed') or 0:.1f}",
        created,
        len(s.get("pull_requests") or []),
        title,
    ))

counts = Counter(s.get("status", "?") for s in sessions)
summary = ", ".join(f"{v} {k}" for k, v in counts.most_common())
print(f"\n{len(sessions)} session(s): {summary}")
