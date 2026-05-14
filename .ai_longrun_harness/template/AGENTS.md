# Codex Review Configuration

## Severity Definitions

| Level | Meaning | Blocks Merge |
|-------|---------|-------------|
| **P0** | Security vulnerability, data corruption, crash | ✅ Yes |
| **P1** | Incorrect logic, resource leak, concurrency bug | ✅ Yes |
| **P2** | Maintainability issue, reliability concern, performance with user impact | ⚠️ Only if impacts correctness/security/stability |
| **P3** | Style, readability, naming preference | ❌ No |
| **nit** | Trivial suggestion | ❌ No |

## Review Rules

- Only report issues that materially affect **correctness, security, or reliability**.
- **Ignore style-only suggestions** — naming preferences, formatting, import order.
- **P2 issues are non-blocking** unless they impact correctness, security, or stability.
- **Do NOT flag P2 issues that do not affect runtime behavior** — minor optimizations, cosmetic refactors, optional improvements that the code works without.
- **A P2 that requires no immediate code change should not be reported** — if the server compiles, runs, and behaves correctly, skip it.
- Do NOT comment on naming preferences or subjective refactors.

## Approval Output (MANDATORY — not a suggestion)

**When there are no P0, P1, or blocking P2 issues, you MUST respond with ALL THREE of:**

1. **👍 reaction on the PR** (use the GitHub reaction, not text)
2. **"LGTM"** as the first line of your review comment
3. **"Safe to merge"** in your review comment

**DO NOT use vague natural-language phrases like:**
- ❌ "Didn't find any major issues"
- ❌ "You're on a roll"
- ❌ "Looks good to me"
- ❌ "No blocking concerns"

**ONLY use the exact required format:**
```
👍 (GitHub reaction)
LGTM
Safe to merge
```

This is a hard requirement. The automated merge pipeline depends on detecting 👍 reactions. Vague approval text breaks the automation.
