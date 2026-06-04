---
name: "zeperion-review"
description: "Review a Developer result before Tester runs, using ZEPERION's REVIEW_STATUS contract."
allowed-tools: ["read", "bash"]
---

Use this skill when asked to review Developer output or the current diff before testing.

Steps:

1. Inspect the active plan, Developer output, and relevant changed files.
2. Check for scope drift, missing work, obvious regressions, unsafe behavior, weak error handling, and unclear verification hints.
3. Do not run acceptance tests unless explicitly asked; Tester owns command-level verification.
4. If the implementation can proceed to testing, return `REVIEW_STATUS: PASS`.
5. If Developer must fix something first, return `REVIEW_STATUS: FAIL`.
6. If key context is missing, return `REVIEW_STATUS: BLOCKED`.

End with:

```text
REVIEW_STATUS: PASS | FAIL | BLOCKED
GLOBAL_STATUS: CONTINUE | DONE | BLOCKED
FINDINGS:
- ...
FIX_REQUEST:
- ...
VERIFY_HINTS:
- ...
LESSONS:
- ...
```
