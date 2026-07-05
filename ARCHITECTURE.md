# Architecture — issue-to-Devin pipeline

A stateless pipeline that turns GitHub issues on `tetratorus/superset` into
Devin sessions, relays the conversation both ways, tracks each issue through
a labeled lifecycle to a merged PR, and can unwind work when an issue is
closed. All pipeline state lives on GitHub; the tracker itself remembers
nothing.

## Issue classes in scope

What this system is *for*, in increasing order of ambition:

**Tier 1 — routine (the realistic target).** Issues with a known shape and a
mechanical fix, where a wrong answer is cheap to catch in review: outdated
dependencies, vulnerability/security-scanner findings, formatting and lint
issues, test coverage gaps, flaky or broken tests, doc fixes, small
well-scoped bugs with a clear reproduction.

**Tier 2 — advanced.** Real bug fixes spanning several files, contained
refactors within one module, performance issues with a measurable target.
These rely on the PR write-up and the human review gate to be trustworthy.

**Tier 3 — idealistic ceiling.** New feature requests, entire modules built
from an issue description. Nothing in the pipeline caps task size, but the
system is not tuned or trusted for this; expect these to land in human
review, not auto-flow.

The tier boundary is enforced at intake by the **triage step**: every
trusted new issue is classified against `knowledge/rubrics/triage.md` before
any fix session spawns — `auto` (Tier 1) flows through, `review` (Tier 2)
flows through with human review forced on the PR, `hold` (Tier 3, security
areas, not-actionable, unverifiable-in-sandbox, or any low-confidence
verdict) gets `devin-hold` + a reason comment and never costs a fix session.

## Components

**tracker.py** — the pipeline loop. Polls GitHub every 5 minutes
(`GH_TRACK_POLL_INTERVAL`, default 300s) and on each cycle does four jobs:

1. *Intake*: triage each new, trusted, untracked issue against the rubric
   (a short classify-only Devin session, terminated after the verdict), then
   spawn a fix session — or hold, spawning nothing.
2. *Relay*: forward new trusted issue comments into the live session.
3. *Watch*: react to session state (waiting for input, suspended, done, PR
   opened) by advancing labels and posting issue comments.
4. *Undo*: detect closed issues and unwind in-flight work.

Single-threaded, no queue: issues are processed sequentially per cycle,
sessions run in parallel on Devin's side, fire-and-forget between cycles.

**playbook.md + knowledge/** — Devin's behavioral contract, version-
controlled here. The playbook carries: post questions and status updates as
issue comments rather than waiting silently; branch naming
`devin/<issue>-<slug>`; PRs reference their issue; scope discipline (fix what
the issue asks, stop and ask on the issue if it balloons); and the PR
write-up template. **sync_playbook.py** pushes playbook and knowledge notes
to the Devin org via the v3 API (create-or-update by name, idempotent) so the
org copy always matches the repo. `knowledge/rubrics/` is deliberately
outside the sync: rubrics steer pipeline decisions (the triage step embeds
`rubrics/triage.md` in its classify prompt directly), not fix-session
behavior.

**sessions.py / status.py** — read-only views. `sessions.py` is
Devin-centric: session id, status, ACUs, PR count per session. `status.py` is
issue-centric: each issue's lifecycle label, session status, PR and CI state,
joined on issue number.

**Dockerfile** — `python:3.12-alpine` + `github-cli`. Entrypoint runs
`sync_playbook.py` first, then the tracker loop. Credentials via env vars.

**Devin GitHub App** (external, one-time setup) — installed on `tetratorus`
with access to `superset`; this is what lets sessions clone, push `devin/*`
branches, and open PRs as `devin-ai-integration[bot]`. The tracker grants
nothing; sessions inherit the org's GitHub integration.

## Flow

```
 ┌─────────────────────────── every 300s ────────────────────────────┐
 │                                                                   │
 │   GitHub (issues, comments, labels)          Devin org (v3 API)   │
 │        ▲            │                            ▲         │      │
 │        │            ▼                            │         ▼      │
 │  ┌────────────────────────── tracker.py ──────────────────────┐   │
 │  │                                                            │   │
 │  │ INTAKE   new open issue, no `devin` label                  │   │
 │  │          ├─ author untrusted ──► label `devin-hold`, stop  │   │
 │  │          ├─ add `devin-triage` label (lock)                │   │
 │  │          ├─ re-check session tag issue:<n> (race backstop) │   │
 │  │          ├─ TRIAGE: classify-only session applies          │   │
 │  │          │    knowledge/rubrics/triage.md, terminated      │   │
 │  │          │    after verdict {decision, rule, confidence}   │   │
 │  │          ├─ hold / low confidence ──► label `devin-hold`,  │   │
 │  │          │    comment reason, NO fix session, stop         │   │
 │  │          ├─ auto | review ──► label `devin`, create fix    │   │
 │  │          │    session: prompt = issue, playbook_id,        │   │
 │  │          │    tag issue:<n>, structured_output_schema      │   │
 │  │          └─ comment "Triage: <verdict> — session <url>"    │   │
 │  │                                                            │   │
 │  │ RELAY    new comment on tracked issue                      │   │
 │  │          ├─ untrusted or bot ──► ignore                    │   │
 │  │          ├─ already has 👀 reaction ──► ignore             │   │
 │  │          └─ POST message to session, then react 👀         │   │
 │  │                                                            │   │
 │  │ WATCH    session status each cycle                         │   │
 │  │          ├─ waiting_for_user + no question posted ──►      │   │
 │  │          │    nudge session: "post your question on issue" │   │
 │  │          ├─ suspended ──► comment reason on issue          │   │
 │  │          ├─ PR opened ──► label `devin-pr`; read           │   │
 │  │          │    structured output ──► `devin-needs-review`   │   │
 │  │          │    or `devin-auto-ok`                           │   │
 │  │          └─ exit/error ──► `devin-done` / `devin-error`    │   │
 │  │                                                            │   │
 │  │ UNDO     tracked issue closed                              │   │
 │  │          ├─ session live ──► terminate it                  │   │
 │  │          ├─ PR open, unmerged ──► close PR, delete branch, │   │
 │  │          │    label `devin-abandoned`                      │   │
 │  │          └─ PR merged ──► comment revert instructions;     │   │
 │  │               never auto-revert                            │   │
 │  └────────────────────────────────────────────────────────────┘   │
 └───────────────────────────────────────────────────────────────────┘

 Devin session (per issue):
   clone repo via GitHub App ─► work ─► questions/updates as issue
   comments ─► push devin/<issue>-<slug> ─► open PR whose body is the
   structured write-up ─► human reviews and merges ─► issue closed

 Startup (container): sync_playbook.py pushes playbook.md + knowledge/
 to the Devin org, then the tracker loop starts.
```

Issue lifecycle — exactly one `devin-*` label at any time:

```
                 ┌─► devin-hold (untrusted author; trusted human can release)
   issue opened ─┤
                 └─► devin-triage (rubric classification in flight)
                     ├─► devin-hold (triage: hold / low confidence —
                     │              no fix session ever spawned)
                     └─► devin ─► devin-pr ─► devin-needs-review ─┐
                         │              └───► devin-auto-ok ──────┼─► merged,
                         │                                        │   issue closed
                         ├─► devin-error (session died)           │
                         └─► devin-abandoned (issue closed early) ┘
```

The PR write-up (every Devin PR body):

```
## What changed              plain-language summary, better than the diff
## Why this is safe to merge
## Needs human review        files + the intent behind each change
## Risks & potential problems
## Tests                     run/passed, coverage delta
```

The same content arrives machine-readable via `structured_output_schema`
(`pr_url`, `needs_human_review`, `review_points[]`, `risks[]`) — that flag,
not prose parsing, is what drives the `devin-needs-review` / `devin-auto-ok`
labels. Downstream, the write-up is the input for a reviewer bot (future)
that checks claims against principles not visible in the diff.

## Decisions

**All pipeline state lives on GitHub.** Labels answer "where is this issue in
the pipeline", the tracker's comments answer "which session", 👀 reactions
answer "which comments were relayed". The tracker starts cold, crashes,
restarts, or runs as accidental duplicates and stays correct — and every bit
of state is visible and overridable in the GitHub UI (pull a label off to
re-route an issue).

**Triage before spend.** The rubric is applied *before* the expensive fix
session exists — a hold costs one short classify-only session (terminated
the moment its verdict is read) instead of a full VM cloning superset. The
rubric is code in this repo (`knowledge/rubrics/triage.md`): changing the
filter's judgment is a reviewable commit, and every verdict is auditable as
a comment on the issue. The playbook's scope-discipline rule remains as the
in-flight backstop for issues that balloon after passing triage.

**Trust is enforced programmatically, not by prompt.** Only authors with
`author_association` OWNER / MEMBER / COLLABORATOR can trigger a session or
have comments relayed. Untrusted text never enters a Devin prompt — issues
are an open channel to an agent with push access, so the gate sits in the
tracker, where a prompt injection can't argue with it.

**Two-layer dedup: label is the lock, session tag is the backstop.** The
`devin` label is written before spawning; every session carries tag
`issue:<n>`, checked before and after taking the label. A duplicate session
requires losing both races within the same few-second window.

**The issue thread is the UI.** Questions from Devin, answers from humans,
session links, suspension notices, revert instructions — all live on the
issue. Nobody needs the Devin console or the dashboards to operate the
pipeline; the dashboards are conveniences, not control surfaces.

**Behavior ships as code.** The playbook and knowledge notes live in this
repo and are synced idempotently at container start — editing Devin's
behavior is a commit, not a console tweak, so it's reviewable and
reproducible.

**Poll, don't push.** Outbound calls only, no webhooks, no inbound ports;
runs from a laptop or any container. Cost: up to one poll interval of latency
per hop, on every hop.

**Merged work is never auto-reverted.** Undo handles cheap reversals
(terminate session, close PR, delete branch). Once code is merged, the
tracker only posts revert instructions — a human decides.

**Failure = retry next cycle.** Every action is idempotent against GitHub
state, so transient failures are logged and naturally retried. A failed spawn
removes the lock label; a failed relay leaves the comment un-reacted. No
alerting — silence in the logs is the only failure signal.

## Credentials

| What                     | Where it's used                               |
|--------------------------|-----------------------------------------------|
| `gh` auth / `GH_TOKEN`   | tracker: polling, labels, comments, reactions, |
|                          | closing PRs, deleting branches                 |
| `DEVIN_SERVICE_USER_KEY` | tracker, sync_playbook, views: Devin v3 API    |
| `DEVIN_ORG_ID`           | scopes all Devin API paths                     |
| Devin GitHub App         | Devin's own repo access (clone/push/PR) —      |
|                          | managed in GitHub settings, not in this repo   |
