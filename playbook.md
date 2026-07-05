# GitHub issue tracker playbook

You are handling issues filed on the `tetratorus/superset` GitHub repository.
Your job is to investigate the issue, implement a fix, and open a pull request.

## Communication norms

- If you need more information, have a question, or want to report progress,
  post a comment on the GitHub issue thread. Do not wait silently in a
  `waiting_for_user` or `waiting_for_approval` state.
- All important status updates (blocked, waiting for a human, suspended, etc.)
  must be visible as issue comments. The issue thread is the UI for this
  pipeline.
- Do not use the Devin console or chat to communicate with the human.

## Branch and PR conventions

- Create a branch named `devin/<issue-number>-<short-slug>` (for example,
  `devin/42-fix-login`).
- The PR title must reference the issue (for example, "Fix login flow (#42)").
- The PR body must contain the structured write-up described below.
- The PR body must also include the text `Fixes #<issue-number>` so GitHub
  links the PR to the issue.

## Scope discipline

- Fix only what the issue asks for.
- If the fix grows beyond the original scope, touches authentication, data
  migrations, security, or public APIs, or you are unsure about the change,
  stop and ask on the issue before continuing.

## Structured PR write-up

Every PR body must contain exactly these sections, in this order:

```markdown
## What changed
Plain-language summary of the change, better than the diff.

## Why this is safe to merge
Reasoning that a reviewer can trust without re-deriving the whole change.

## Needs human review
Files and the intent behind each change that a human should judge.

## Risks & potential problems
What could go wrong, edge cases, and assumptions made.

## Tests
What was run, what passed, and coverage delta if known.
```

## Structured output

When you are done, also produce a JSON object matching the schema provided in
`structured_output_schema` for this session. It must include at least:

- `pr_url`: the URL of the PR you opened.
- `needs_human_review`: true if a human should review the PR before merging.
- `review_points`: a list of files or changes that need human judgment.
- `risks`: a list of risks or potential problems.
