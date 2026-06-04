---
name: "zeperion-test"
description: "Run or interpret verification for a ZEPERION Tester response."
allowed-tools: ["read", "bash"]
---

Use this skill when asked to verify a ZEPERION implementation.

Steps:

1. Read the plan, Developer output, Reviewer output, and configured verification commands.
2. Run the requested verification commands exactly when asked.
3. Treat command exit codes and output as the source of truth.
4. If commands are absent, explain that the verdict is based on code/output inspection only.
5. Produce the exact Tester markers:

```text
TEST_STATUS: PASS | FAIL | ERROR
GLOBAL_STATUS: CONTINUE | DONE | BLOCKED
TEST_CASES:
- ...
BUGS:
- ...
FIX_REQUEST:
- ...
LESSONS:
- ...
```

Do not mark PASS if tests failed, timed out, or were not actually run.
