# Visualization plan — pipeline dashboard

Goal: a single self-contained HTML page that shows the whole
issue→Devin→PR pipeline at a glance — current state, drift, cost, ROI,
and trends. This document is the plan: where the state lives, how to
gather it, what can go stale, and what the page shows.

## 1. Where state lives

There is no database. Everything the dashboard needs is spread across
three places, and one class of data exists nowhere at all (see §3).

| State | Lives in | Fetched via | Notes |
|---|---|---|---|
| Issue lifecycle position | GitHub labels (`devin`, `devin-hold`, `devin-pr`, `devin-needs-review`, `devin-auto-ok`, `devin-error`, `devin-abandoned`) | `gh api /repos/O/R/issues` | The canonical "where is this issue" answer. Humans can edit labels — that's a feature, not corruption. |
| Lifecycle *history* (when each label was added/removed) | GitHub issue timeline events | `gh api /repos/O/R/issues/<n>/timeline` | `labeled`/`unlabeled` events carry timestamps + actor. This is how trends get reconstructed without a database. |
| Issue ↔ session link | Tracker comment ("Devin session started: <url>") + session tag `issue:<n>` | comments API / Devin sessions list | Join key in both directions. |
| Relay progress | 👀 reactions on comments | reactions API | Only matters for "is the relay healthy", not for metrics. |
| Session status, ACUs, PRs | Devin v3 API (`/organizations/$ORG/sessions`) | `sessions?limit=100` | Fields seen in code: `session_id`, `status`, `status_detail`, `acus_consumed`, `created_at`, `updated_at`, `tags`, `title`, `pull_requests`, `url`. `acus_consumed` is a **cumulative counter with no history**. |
| Structured output (`needs_human_review`, `review_points[]`, `risks[]`) | Devin session structured output | session detail endpoint | Also mirrored as labels once section 6/7 of TODO lands. |
| PR state, merge time, branch | GitHub PRs | `gh api /repos/O/R/pulls?state=all` (filter `devin/*` branches / bot author) | Merge timestamps are the "value delivered" clock. |
| CI results / coverage | GitHub Actions on the fork | checks API | **Doesn't exist yet** (TODO §9 open item). Plan for the panel, render "no CI" until then. |
| Cost in dollars | Nowhere | config knob `ACU_USD` | Devin bills per ACU; the rate depends on plan. Don't hardcode — make it a config value with the plan rate filled in by hand. |

## 2. Gathering: one collector, one JSON blob

`dashboard.py` (extends the planned `status.py`) does one fetch pass and
emits a single `state.json`:

1. `gh api` issues (open **and closed** — closed issues are where ROI
   lives) — one page call.
2. `gh api` pulls, state=all — one call.
3. Devin sessions list — one call.
4. Per tracked issue: timeline events (for label transition history).
   This is the only N-call step; cache by `updated_at` so unchanged
   issues aren't re-fetched.
5. Join on issue number (tag `issue:<n>` ↔ labels ↔ PR body/branch name).

Then it renders `dashboard.html` with the JSON embedded inline (no
server, no CDN, no fetch — open the file and it works). Refresh = rerun
the script. In the container, run it at the end of each tracker poll
cycle or on its own loop; either way it's read-only and stateless like
everything else.

Rejected alternative: a live server/webhook dashboard. More moving
parts, violates the poll-only/no-inbound-ports decision, and 5-minute
granularity is already the pipeline's native tick — a static regenerated
page loses nothing.

## 3. Staleness — what can lie, what is lost forever

Three distinct failure modes; the page should treat them differently.

**a. Snapshot age (benign, must be visible).** The page is as fresh as
its last generation. Stamp `generated_at` prominently; JS tints it red
when the page is older than ~2 poll intervals — that doubles as the
tracker-liveness heartbeat we don't otherwise have.

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
- `acus_consumed` over time: **not recoverable** — the API only gives
  the current cumulative number per session. Same for status flapping
  (running → waiting → running).

Fix for (c): the collector appends one line per run to
`snapshots.jsonl` — `{ts, per-session: {status, acus}}`. This is
dashboard-local convenience state, not pipeline state; deleting it never
affects correctness, it only flattens the ACU burn chart back to
per-session totals. That keeps the "all pipeline state lives on GitHub"
decision intact.

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
- ACU burn per day (from snapshots.jsonl deltas; before snapshots
  accumulate, approximate by spreading each session's total across its
  created→updated window).
- Cost per merged PR, weekly — the "is this getting cheaper" line.

## 5. The page

Single HTML file, inline CSS/JS, embedded JSON, no external deps.
Top-to-bottom:

1. **Header** — repo, generated-at stamp (staleness-tinted), totals
   strip: open issues, live sessions, total ACUs, total $, cost per
   merged PR.
2. **Funnel** — the lifecycle as a left-to-right bar with counts at
   each stage and drop-off percentages; failure states (`hold`,
   `error`, `abandoned`) hang below the stage where they exit.
3. **Drift panel** — inconsistencies from §3b. Green "no drift" line
   when empty.
4. **In-flight table** — issue #, title, lifecycle label, session
   status, ACUs so far, age in current state, links (issue, session,
   PR). Sorted by "needs attention first" (waiting_for_user, then
   drift, then age).
5. **Cost & value panel** — exact costs on the left, value proxies on
   the right (labeled as proxies), plus ACU-per-session histogram.
6. **Trends** — three small time-series charts (issues/PRs per day,
   ACU burn, cost per merge). Inline SVG, no chart library.
7. **Completed table** — merged/closed issues with final cost, lead
   time, review path (auto-ok vs needs-review).

## 6. Build order (each step ships working)

1. `dashboard.py` collector: issues + sessions + PRs → `state.json`,
   plus the §4 counters that need no history. Print them as text first
   (this subsumes `status.py` from TODO §9).
2. Render pass: embed JSON → `dashboard.html` with header, funnel,
   in-flight table. Static, ugly, correct.
3. Drift detection + panel.
4. Timeline reconstruction → lead times, per-day trend charts.
5. `snapshots.jsonl` appender → ACU burn chart.
6. CI/quality panel — blocked on GitHub Actions existing on the fork;
   stub until then.

Open items to confirm before step 1:
- Exact Devin v3 session fields (does the list endpoint return
  `status_detail` and `updated_at`? does structured output require the
  per-session detail call?) — verify against the live API, don't assume.
- `ACU_USD` value for the current plan.
