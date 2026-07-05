# Scope discipline

When working on an issue, keep the change focused on the problem described.

Stop and ask on the issue thread if:

- The fix starts touching unrelated subsystems.
- The change involves authentication, authorization, data migrations, or
  public API contracts.
- You are unsure whether the fix is safe or correct.
- The requested change is vague or could be interpreted in multiple ways.

It is better to confirm scope with a human than to ship a large or risky change
without review.
