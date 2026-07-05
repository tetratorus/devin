# PR write-up guidance

Use the PR write-up format from the playbook. The goal is to give a human
reviewer enough context to judge the change without having to re-derive the
entire solution from the diff.

For `Needs human review`, focus on the *intent* behind the change, not just the
file names. A reviewer should understand why each change was necessary and
what could go wrong.

For `Tests`, be honest. If tests were not run or not applicable, say so. If
you added or fixed tests, describe what they cover.
