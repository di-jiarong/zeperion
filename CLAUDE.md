# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

ZEPERION — a multi-agent development & PR delivery pipeline harness. This is the **source template repo**; it gets copied into target projects via `.ai_longrun_harness/template/zeperion-init.sh`. The target project then runs `/zeperion` and `/zeperion-pr` slash commands to execute the full Planner → Developer → Tester → PR → Codex → Merge workflow.

There is no app, build, or test suite in this repo. The harness consists of bash scripts, prompt templates, state file schemas, and Claude Code configuration files.

## Directory structure

```
.ai_longrun_harness/
  run_multi_agent_loop.sh    # Multi-agent: Planner → Developer → Tester loop
  run_ralph_loop.sh           # Single-agent: simple task queue loop
  run_pr_pipeline.sh          # PR delivery: commit → push → PR → Codex → merge
  reset_state.sh              # Backup + reset all state files to idle
  config.env.example          # All env vars with defaults
  requirement_template.txt    # MoSCoW-style requirement spec template
  prompts/
    master_scheduler_prompt.txt  # Master Scheduler role (scheduling only)
    planner_prompt.txt           # Planner role (Explorer agent, outputs plan)
    developer_prompt.txt         # Developer role (implements, outputs changes)
    tester_prompt.txt            # Tester role (verifies, outputs PASS/FAIL)
    pr_fixer_prompt.txt          # PR Fixer role (processes Codex comments)
  state/                         # Runtime state (not committed to git)
    workflow_state.json          # Current phase/round/fix_attempt/owner
    pipeline_state.json          # PR number/Cron ID/review round
    progress.json                # Overall progress summary
    current_plan.txt             # Planner output
    task_result.txt              # Developer output
    test_report.txt              # Tester output
    lessons_learned.txt          # Accumulated cross-round experience
    codex_comments.txt           # Collected Codex review comments
    errors.log                   # Failure event log
  template/                      # Files copied into target projects
    zeperion-init.sh             # Init script (copies config into target project)
    CLAUDE.md                    # Multi-agent workflow protocol (for target)
    AGENTS.md                    # Codex review rules (P0/P1/P2/P3 severity)
    settings.json                # Claude Code config (API, models, commands)
    .settings.local.json         # 60+ pre-configured permission rules
    zeperion.md                  # /zeperion slash command definition
    zeperion-pr.md               # /zeperion-pr slash command definition
    zeperion-flowchart.md        # Full flowchart documentation
```

## The three scripts

### `run_multi_agent_loop.sh`
Orchestrates a Planner → Developer → Tester loop. Sources `config.env`, initializes state files if missing/corrupt, then loops up to `MAX_ROUNDS` (default 50). Each round:
1. **Planner** — builds prompt from template + current state files, calls the LLM CLI via `MULTI_AGENT_CALL_TEMPLATE`, extracts `TASK_ID` from output
2. **Developer** — implements plan, writes to `task_result.txt`
3. **Tester** — verifies, writes `TEST_STATUS: PASS` or `FAIL`
4. If FAIL → increment `fix_attempt`, loop back to Developer (max `MAX_FIX_ATTEMPTS`, default 3)
5. If PASS → extract LESSONS from both outputs, append to `lessons_learned.txt`
6. Check for `GLOBAL_STATUS: DONE` in plan to exit; otherwise next round

Uses `trap ... ERR` to catch failures and write crashed state. Role calls use `run_role_with_retry` (default 2 retries). `CONTINUE_ON_ROLE_ERROR` controls whether to skip failed roles or exit.

### `run_ralph_loop.sh`
Simple single-agent loop. Reads tasks from `task_queue.txt` (one per line), pops the first, builds a prompt, calls the LLM via `SINGLE_AGENT_CALL_TEMPLATE`, extracts any `NEW_TASKS` and appends them to the queue. Runs up to `MAX_CYCLES` (default 200). Useful for simple sequential task execution.

### `run_pr_pipeline.sh`
Post-development delivery pipeline:
1. Commits all changes (Conventional Commits format), pushes to remote
2. Creates PR with `gh pr create` targeting `PR_TARGET_BRANCH` (default `dev`), labeled `automerge`
3. Collects all Codex comments using `gh api --paginate` + `?per_page=100`
4. Counts Codex 👍 reactions across PR, issue comments, and review comments
5. Decision: Codex approved → enable auto-merge; Codex rejected → save comments, exit for manual fix; waiting → report status

## Config system

`config.env` (sourced by all scripts, gitignored) overrides defaults from `config.env.example`. Key variables:

| Variable | Purpose |
|----------|---------|
| `MULTI_AGENT_CALL_TEMPLATE` | Shell template for invoking LLM CLI per role |
| `SINGLE_AGENT_CALL_TEMPLATE` | Shell template for single-agent mode |
| `MASTER_MODEL` / `PLANNER_MODEL` / `DEVELOPER_MODEL` / `TESTER_MODEL` | Per-role model selection |
| `MAX_ROUNDS` / `MAX_FIX_ATTEMPTS` / `CONTINUE_ON_ROLE_ERROR` | Loop control |
| `GITHUB_REPO` / `GITHUB_TOKEN` / `PR_TARGET_BRANCH` | PR pipeline config |

## State file design

All inter-agent state is file-driven — no in-memory state between invocations. Each script initializes JSON state files if missing or malformed (checked via `jq empty`). The `reset_state.sh` script backs up current state to `state/backups/<timestamp>/`, then resets everything to `idle`/`init`, preserving only `lessons_learned.txt`.

Three state files track different concerns:

### `progress.json` (top-level coordinator)
Tracks which mode is running and overall status. Contains only:
- `mode`: "multi_agent" | "single_agent_ralph" | "pr_pipeline"
- `status`: "idle" | "running" | "completed" | "failed" | "crashed"
- `updated_at`: ISO 8601 timestamp

**Design principle**: Does NOT duplicate fields from workflow_state or pipeline_state. To get detailed state (round, phase, etc.), read the appropriate state file based on `mode`.

### `workflow_state.json` (multi-agent loop internals)
Tracks the Planner → Developer → Tester loop state:
- `status`: current status
- `phase`: "planning" | "developing" | "testing" | "reviewing" | "fix"
- `round`: current iteration number
- `task_id`: current task identifier
- `fix_attempt`: number of fix attempts in current round
- `owner_dev_session`, `owner_test_session`: session IDs
- `last_error`: error message if any
- `updated_at`: ISO 8601 timestamp

### `pipeline_state.json` (PR delivery flow)
Tracks the commit → push → PR → review → merge pipeline:
- `status`: current status
- `phase`: "commit" | "push" | "pr_created" | "review" | "merge"
- `pr_branch`, `pr_target`: branch names
- `pr_number`, `pr_url`: PR identifiers
- `codex_status`: Codex review result
- `updated_at`: ISO 8601 timestamp

**No field overlap**: Each file owns its domain. `progress.json` provides the entry point; detailed state lives in the domain-specific files.

## Prompt output format contract

Each role prompt enforces a strict output format parsed by the scripts:

- **Planner**: `TASK_ID:`, `GLOBAL_STATUS: RUNNING | DONE`, `PLAN:`, `RISKS:`, `HANDOFF_TO_DEVELOPER:`
- **Developer**: `DEV_STATUS: DONE | BLOCKED`, `CHANGES:`, `VERIFY_HINTS:`, `BLOCKERS:`, `LESSONS:`
- **Tester**: `TEST_STATUS: PASS | FAIL`, `TEST_CASES:`, `BUGS:`, `FIX_REQUEST:`, `LESSONS:`
- **PR Fixer**: `FIX_STATUS: DONE | PARTIAL | BLOCKED`, `FIXED_ISSUES:`, `FALSE_POSITIVES:`, `REMAINING:`, `LESSONS:`

Scripts parse these with `awk` patterns (e.g., `awk -F': ' '/^TEST_STATUS:/{print $2}'`) and `rg -q` for status checks.

## Critical rules (learned from past incidents, encoded in STATE_MANAGEMENT.md and lessons_learned.txt)

1. **GitHub API pagination**: Always use `gh api "...?per_page=100"` or `gh api --paginate` for comments endpoints. Default 30-item pages truncate results when comments exceed 30 — this caused PR #4 to miss 46/76 comments for 9 review rounds.
2. **One Cron per PR**: Always `CronList` + `CronDelete` old tasks before `CronCreate`. Cron prompt must self-delete on 👍 detection.
3. **Batch fix strategy**: Collect ALL Codex comments → fix everything once → single push. Never fix one comment → push → fix another → push.
4. **Don't re-trigger @codex review prematurely**: After push + `@codex review`, wait at least 1 hour before re-triggering. Multiple triggers on intermediate commits cause duplicate review rounds.

## Editing the harness

When modifying shell scripts: they use `set -euo pipefail`, `shellcheck` directives, and `trap ... ERR`. The LLM CLI invocation now uses structured configuration (`LLM_CLI_COMMAND`, `LLM_CLI_ARGS`, etc.) to avoid `eval` security risks. The legacy `MULTI_AGENT_CALL_TEMPLATE` is still supported for backward compatibility but not recommended.

When modifying prompts: the output format markers (e.g., `TEST_STATUS:`, `LESSONS:`) are parsed by `awk`/`rg` in the scripts. Changing format strings requires updating both the prompt template and the parsing logic in the corresponding `.sh` file.

When modifying the template files in `template/`: these are what get copied into target projects by `zeperion-init.sh`. The `template/CLAUDE.md` is the workflow protocol for the target project's Claude instance, **not** for this repo itself.
