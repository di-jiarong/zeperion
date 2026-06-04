---
name: "zeperion-checkpoint"
description: "Create a lightweight git checkpoint before risky edits or shell commands."
allowed-tools: ["bash"]
---

Use this skill before destructive, broad, or hard-to-reverse changes.

Steps:

1. Run `git status --short` to inspect the working tree.
2. If there are existing user changes, do not overwrite or revert them.
3. Create a checkpoint using one of:
   - `git stash push -u -m "zeperion checkpoint: <reason>"` when the user wants a stash.
   - `git branch codex/checkpoint-<short-id>` when preserving the current HEAD is enough.
4. Proceed only after the checkpoint exists.
5. If a command fails and rollback is requested, explain what will be restored before applying it.

Never run `git reset --hard` or discard user changes unless the user explicitly asks.
