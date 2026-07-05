# devin — GitHub issue → Devin pipeline

[Loom](https://www.loom.com/share/74d594053a604bb89587861c6113ddb8)

Watches a GitHub repo for new issues and hands each one to a
[Devin](https://devin.ai) session that fixes it and opens a PR. All pipeline
state lives on GitHub itself (labels, comments, reactions) — the tracker is
stateless, safe to restart, and safe to run twice by accident.

## Run it

```bash
docker build -t devin-tracker .

docker run -d --name devin-tracker \
  -e DEVIN_SERVICE_USER_KEY=cog_...     `# Devin service-user key (Settings > Service Users)` \
  -e DEVIN_ORG_ID=org-...               `# Devin organization ID` \
  -e GH_TOKEN=$(gh auth token)          `# any GitHub token with repo scope` \
  devin-tracker
```

That's it. The container first syncs `playbook.md` + `knowledge/` to the
Devin org (so sessions get the behavioral contract), then polls for issues.

Defaults target `tetratorus/superset` every 300s; override with:

| Env var                  | Default              |
|--------------------------|----------------------|
| `GH_TRACK_OWNER`         | `tetratorus`         |
| `GH_TRACK_REPO`          | `superset`           |
| `GH_TRACK_POLL_INTERVAL` | `300` (seconds)      |

One-time external setup (not in this repo): the Devin GitHub App must have
access to the target repo — that's what lets sessions push branches and open
PRs.

## What happens to an issue

1. **Intake** — new open issue from a trusted author (`author_association`
   OWNER/MEMBER/COLLABORATOR) gets locked with `devin-triage`. Untrusted
   authors get `devin-hold`; their text never reaches a prompt.
2. **Triage** — a short classify-only session applies
   `knowledge/rubrics/triage.md`: `auto` → fix session spawns; `review` →
   spawns with human review forced on the PR; `hold` (proposals/SIPs, auth/
   security areas, vague or sandbox-unverifiable issues, low confidence) →
   `devin-hold` + reason comment, **no fix session, no ACUs burned**. Every
   verdict is auditable on the issue thread.
3. **Work** — Devin fixes the issue on branch `devin/<n>-<slug>` and opens a
   PR whose body is a structured write-up (what changed / why safe / what
   needs human review / risks / tests), also returned machine-readably via
   structured output.
4. **Conversation** — trusted comments on the issue are relayed into the
   live session (👀 reaction marks them relayed); Devin posts questions and
   status back as issue comments. Blocked/suspended sessions are surfaced on
   the issue.
5. **Lifecycle labels** — the issue always carries exactly one state label:
   `devin-triage` → `devin` → `devin-pr` → `devin-needs-review` | `devin-auto-ok`, with
   failure branches `devin-hold` / `devin-error` / `devin-abandoned`.
   `devin-ci-failed` rides alongside when PR checks go red (failures are
   relayed into the session for self-fix).
6. **Undo** — closing an issue mid-flight terminates its session, closes the
   PR, deletes the branch (`devin-abandoned`). Merged work is never
   auto-reverted; the tracker comments revert instructions instead.

## Repo layout

| Path                        | What                                          |
|-----------------------------|-----------------------------------------------|
| `tracker.py`                | the pipeline loop (intake/relay/watch/undo)   |
| `playbook.md`, `knowledge/` | Devin's behavioral contract, synced as code   |
| `knowledge/rubrics/`        | decision rubrics (issue triage) — not synced  |
| `sync_playbook.py`          | idempotent playbook/knowledge sync            |
| `sessions.py`               | CLI table of Devin sessions                   |
| `collector.py`, `serve.py`, `dashboard.html` | status dashboard (separate; run locally: `collector.py` + `serve.py`, then http://localhost:8400) |
| `TODO`                      | build plan the executing agent works through  |
| `ARCHITECTURE.md`           | full design + flow diagram                    |
