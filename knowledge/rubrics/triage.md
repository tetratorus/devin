# Triage rubric — should this issue be handed to an autonomous coding agent?

Classify every issue into exactly one decision:

- **auto**   — hand to the agent, normal review.
- **review** — hand to the agent, but the resulting PR requires human review
               regardless of what the agent reports.
- **hold**   — do NOT hand to the agent. A human triages it first.

Work through the rules in order; the first rule that fires decides.

## 1. Proposals and features are never agent work -> hold

Design proposals, SIPs, RFCs, feature requests, "add support for X",
new modules, new chart types, new integrations. Signals: labels like
`sip`, `design:proposal`, `enhancement`; titles starting "Proposal",
"[SIP-...]", "Feature request". Size is irrelevant — even a small feature
request is a product decision, not a fix.

## 2. Security-sensitive areas -> hold

Anything touching authentication, authorization, session handling, SSO,
API keys, secrets, or CSP/security headers. Signals: labels or text
mentioning auth, SSO, OAuth, Keycloak, JWT, API keys, permissions, roles.
These need human judgment even when the fix looks mechanical.

## 3. Not actionable yet -> hold

The issue lacks what any engineer would need to start: no reproduction
steps, intermittent with no pattern, "no errors in logs", missing
version/config info, or it is really a support question / probable
misconfiguration. Signals: `validation:required`-style labels, questions
rather than defect reports.

## 4. The agent must be able to verify its own fix -> hold if it can't

If reproducing or verifying requires infrastructure the agent cannot
stand up in its sandbox (external SSO providers, TLS-enabled Redis
Sentinel clusters, specific proprietary databases, customer-specific
deployments), hold — an unverifiable fix from an agent is a liability.
Plain unit-testable code paths and UI behavior reproducible in a local
dev build are fine.

## 5. Risky-but-scoped changes -> review

The issue is a genuine, reproducible, scoped defect but the fix will
touch a danger zone: database migrations or schema, cross-cutting
dashboard/filter state, data caching semantics, performance work,
anything spanning several subsystems. Hand it to the agent, but force
human review of the PR.

## 6. Everything else that is a clear, scoped, reproducible defect -> auto

Known-shape fixes: a wrong conditional, a leaked DOM node, a bad default,
an incorrect header, lint/format/type errors, broken or flaky tests,
outdated dependencies, doc corrections. Clear reproduction, contained
blast radius, verifiable with tests or a local run.

## Tie-breakers

- Unsure between review and auto -> review.
- Unsure between hold and anything -> hold.
- Confidence is part of the output: if your confidence is low, say so —
  low-confidence classifications are treated as hold by the pipeline.
