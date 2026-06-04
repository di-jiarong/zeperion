---
name: "zeperion-ship"
description: "Prepare a clean ZEPERION PR delivery: inspect changes, avoid runtime artifacts, and use a conventional commit subject."
allowed-tools: ["read", "bash"]
---

Use this skill when asked to prepare local changes for delivery.

Steps:

1. Run `git status --short`.
2. Inspect staged and unstaged business changes.
3. Ensure `.zeperion/state`, `.zeperion/logs`, checkpoints, and runtime artifacts are not staged.
4. Prefer the Planner `PR_TITLE` as the commit subject when available.
5. Otherwise derive a Conventional Commits subject such as `feat: add reviewer gate`.
6. Summarize changed files and verification status before committing.

If committing is requested, use:

```bash
git add -A
git reset HEAD -- .zeperion/state .zeperion/logs || true
git commit -m "<conventional subject>"
```

Never fabricate verification results.
