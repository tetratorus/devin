# Visualization plan — pipeline dashboard

Goal: an auto-updating HTML dashboard that shows the whole
issue→Devin→PR pipeline at a glance — current state, drift, cost,
value proxies, and trends. A collector loop captures everything into
SQLite; a tiny Python server serves the page and its metrics from the
DB. This document is the plan: where the state lives, how to gather
it, what can go stale, and what the page shows.

## 1. Where state lives

There is no database. Everything the dashboard needs is spread across
three places, and one class of data exists nowhere at all (see §3).

| State | Lives in | Fetched via | Notes |
|---|---|---|---|
| Issue lifecycle position | GitHub labels (`devin-triage`, `devin`, `devin-hold`, `devin-pr`, `devin-needs-review`, `devin-auto-ok`, `devin-error`, `devin-abandoned`; flag labels `devin-ci-failed`, `devin-review-required` ride alongside) | `gh api /repos/O/R/issues` | The canonical "where is this issue" answer. Humans can edit labels — that's a feature, not corruption. Triage sessions are tagged `triage:<n>`, never `issue:<n>`, so they don't join to issues. |
| Lifecycle *history* (when each label was added/removed) | GitHub issue timeline events | `gh api /repos/O/R/issues/<n>/timeline` | `labeled`/`unlabeled` events carry timestamps + actor. This is how trends get reconstructed without a database. |
| Issue ↔ session link | Tracker comment ("Devin session started: <url>") + session tag `issue:<n>` | comments API / Devin sessions list | Join key in both directions. |
| Relay progress | 👀 reactions on comments | reactions API | Only matters for "is the relay healthy", not for metrics. |
| Session status, ACUs, PRs | Devin v3 API (`/organizations/$ORG/sessions`) | `sessions?limit=100` | Fields seen in code: `session_id`, `status`, `status_detail`, `acus_consumed`, `created_at`, `updated_at`, `tags`, `title`, `pull_requests`, `url`. `acus_consumed` is a **cumulative counter with no history**. |
| Structured output (`needs_human_review`, `review_points[]`, `risks[]`) | Devin session structured output | session detail endpoint | Also mirrored as labels once section 6/7 of TODO lands. |
| PR state, merge time, branch | GitHub PRs | `gh api /repos/O/R/pulls?state=all` (filter `devin/*` branches / bot author) | Merge timestamps are the "value delivered" clock. |
| CI results / coverage | GitHub Actions on the fork | checks API | **Doesn't exist yet** (TODO §9 open item). Plan for the panel, render "no CI" until then. |
| Cost in dollars | Nowhere | config knob `ACU_USD` | Devin bills per ACU; the rate depends on plan. Don't hardcode — make it a config value with the plan rate filled in by hand. |

## 2. Gathering: collector → SQLite → server → auto-updating HTML

Three pieces, one file each:

**Collector** (`collector.py`) — a loop on the poll interval. Each
cycle it fetches:

1. `gh api` issues (open **and closed** — closed issues are where the
   value proxies live) — one page call.
2. `gh api` pulls, state=all — one call.
3. Devin sessions list — one call.
4. Per tracked issue: timeline events (for label transition history).
   This is the only N-call step; skip issues whose `updated_at` hasn't
   moved.

and writes everything into SQLite (`pipeline.db`).

**Capture-everything schema.** Don't design per-metric tables — store
the raw payloads and derive at read time, so future consumers (new
panels, or a Devin session answering ad-hoc questions against the DB)
aren't limited by today's column choices:

```
observations(fetched_at, source, kind, entity_key, payload_hash, payload_json)
  -- append-only. source: github|devin. kind: issue|pr|session|timeline_event.
  -- entity_key: issue number, PR number, session_id.
  -- skip the insert when payload_hash equals the entity's latest hash,
  -- so history rows appear exactly when something changed.
polls(started_at, finished_at, ok, notes)
  -- one row per collector cycle; also the liveness heartbeat.
```

"Latest state" is a view (max `fetched_at` per entity); history — ACU
burn, status flapping, label churn — is the append trail. SQLite's
JSON functions query straight into `payload_json`, so nothing needs a
migration when the Devin API grows a field.

**Server** (`serve.py`) — stdlib `http.server`, no framework. Two
routes: `/` serves `dashboard.html`; `/api/state` runs the metric
queries against SQLite and returns one JSON blob. Read-only.

**Page** — `dashboard.html` fetches `/api/state` every ~30s and
re-renders. Freshness is still bounded by the collector's poll
interval; the fast page poll just picks new rows up promptly.

The DB is derived, disposable state — not pipeline state. GitHub
remains the source of truth; delete `pipeline.db` and the collector
rebuilds everything except observation history (which only flattens
the trend charts). The tracker never reads it.

Future hook (explicitly a goal of capture-everything): "ask the
pipeline a question" — spawn a Devin session pointed at `pipeline.db`
(or a SQL endpoint on the server) so ad-hoc questions don't need a new
dashboard panel.

## 3. Staleness — what can lie, what is lost forever

Three distinct failure modes; the page should treat them differently.

**a. Data age (benign, must be visible).** The page auto-refreshes,
but the data is only as fresh as the last successful collector cycle.
Show the last `polls` row's timestamp prominently; tint it red when
older than ~2 poll intervals — that's the collector-liveness
heartbeat, and a stale collector is also the best available hint that
the tracker (same host, same credentials) may be down too.

**b. Drift between the two sources of truth (the interesting one).**
Labels (GitHub) and sessions (Devin) are written by the same tracker but
can disagree: label `devin` with no tagged session (spawn failed after
lock), live session on a closed issue (undo hasn't run), `devin-pr`
label but PR closed by a human, session `waiting_for_user` with no
question comment on the issue. These mismatches are exactly the bugs the
tracker is supposed to prevent — the dashboard's job is to surface them,
so a dedicated **Drift panel** lists every issue where the join is
inconsistent. Empty drift panel = pipeline healthy.

**c. History that is never recorded (must be captured or lost).**
- Label transitions: recoverable from timeline events. ✅
- Issue/PR open/close/merge times: recoverable. ✅
- `acus_consumed` over time: **not recoverable from the API** — it
  only gives the current cumulative number per session. Same for
  status flapping (running → waiting → running).

The `observations` table is the fix for (c): because the collector
appends a row whenever a session's payload changes, the ACU time
series and status history accrue automatically from the moment the
collector first runs. Anything before that is gone — so per-session
ACU totals are exact from day one, but burn *charts* only cover the
DB's lifetime.

## 4. Metrics — cost and ROI proxies

True ROI is not measurable here: the value of a merged fix (engineer
hours saved, bugs prevented) never appears in any API. What we *can* do
is capture the cost side exactly (ACUs are billed and reported) and the
value side through proxies — observable facts that imply value. The
page should label them as proxies, not dress them up as ROI.

The question the page must answer: **what are we paying per merged
fix, and is it trending the right way?**

Funnel (counts + conversion %):
```
issues opened → trusted/held → session spawned → PR opened → merged
```

Cost:
- Total ACUs (all sessions), × `ACU_USD` → total spend.
- ACUs per session: median + distribution (a histogram exposes runaway
  sessions instantly).
- **Cost per merged PR** = total spend ÷ merged Devin PRs. The headline
  number.
- **Wasted spend** = ACUs on sessions that ended in error / abandoned /
  no PR ever opened. Waste ratio = wasted ÷ total.

Value proxies (each observable today, each implying value without
claiming to measure it):
- **Throughput**: issues resolved by merged Devin PRs — count, and % of
  all issues closed in the window. Every one is a task no human picked
  up.
- **Survival rate**: merged ÷ spawned. A merge means the work passed a
  human review gate — the strongest value signal we have.
- **Lead time**: issue opened → PR opened → merged (medians). Compare
  against the poll-interval floor (~3 hops × 5 min minimum). Fast
  turnaround on routine issues is itself the product.
- **Human touch**: merged via `devin-auto-ok` vs `devin-needs-review`,
  and whether humans pushed commits to the Devin branch before merging
  (extra commits on a `devin/*` branch by non-bot authors = rework, a
  negative proxy).
- **Stickiness**: merged Devin PRs later reverted, or their issues
  reopened (searchable via timeline events) — the "did the fix hold"
  signal.
- Later, when CI exists: tests fixed/broken, coverage delta per PR.

Trends (needs timeline reconstruction + snapshots):
- Issues opened / sessions spawned / PRs merged per day.
- ACU burn per day (deltas between session observations; for sessions
  that predate the DB, approximate by spreading the total across the
  created→updated window).
- Cost per merged PR, weekly — the "is this getting cheaper" line.

## 5. The page

Single HTML file, inline CSS/JS, no external deps; polls `/api/state`
every ~30s and re-renders in place. Styled after Apache Superset
(teal `#20A7C9` primary, their status colors, Inter); light/dark
follow the OS via `prefers-color-scheme`. Organizing idea: the
pipeline only ever needs two human actions — merge a PR or answer a
question — so those lead. Top-to-bottom:

1. **Masthead + KPI row** — repo, data-age stamp (staleness-tinted);
   tiles: issues raised, in transit, awaiting review, merged, spend,
   cost per merge.
2. **Needs a human** — the action queues: open Devin PRs (review &
   merge) and sessions waiting for input.
3. **Pipeline board** — kanban-style columns Raised → In session →
   PR open → Done with counts in the headers; "in transit" = the
   middle two.
4. **Cost & value panel** — exact costs on the left, value proxies on
   the right (labeled as proxies).
5. **Trends** — small daily charts (issues opened, PRs merged, ACU
   burn). Inline SVG, no chart library.
6. **Collapsed footnotes** — all-issues table, unlinked sessions,
   consistency checks (the §3b drift list, demoted: still computed
   because it catches the pipeline lying, but not a headline panel).

## 6. Build order (each step ships working)

1. `collector.py`: one fetch pass (issues + sessions + PRs) →
   `observations` + `polls` in SQLite, then loop. Verify with a
   sqlite3 query, no UI yet (this subsumes `status.py` from TODO §9).
2. `serve.py` + `dashboard.html`: `/api/state` computing the
   no-history metrics from the latest-state view; page with header,
   funnel, in-flight table, 30s auto-refresh. Static, ugly, correct.
3. Drift detection + panel.
4. Timeline events into the collector → lead times, per-day trend
   charts.
5. ACU burn chart from observation deltas (data has been accruing
   since step 1 — no new collection needed).
6. CI/quality panel — blocked on GitHub Actions existing on the fork;
   stub until then.

Open items to confirm before step 1:
- Exact Devin v3 session fields (does the list endpoint return
  `status_detail` and `updated_at`? does structured output require the
  per-session detail call?) — verify against the live API, don't assume.
- `ACU_USD` value for the current plan.
