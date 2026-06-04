---
name: "zeperion-plan"
description: "Create a ZEPERION-compatible Planner response from the current requirement and project context."
allowed-tools: ["read", "bash"]
---

Use this skill when asked to plan a ZEPERION development round.

Steps:

1. Read the requirement, current project context, and any prior Planner/Tester output if available.
2. Break the work into the smallest executable, testable task for this round.
3. Prefer low-risk sequencing and explicit acceptance criteria.
4. End with the exact ZEPERION Planner markers:

```text
TASK_ID: task_xxx
PR_TITLE: feat|fix|chore|refactor|docs|test: short title
GLOBAL_STATUS: CONTINUE | DONE | BLOCKED
PLAN:
- [P1] ...
RISKS:
- ...
HANDOFF_TO_DEVELOPER:
- ...
LESSONS:
- ...
```

Do not implement code while using this skill.
