# CLAUDE.md

This file orients AI coding assistants working in this repository. Keep it in sync with the actual code; if you find it lying, fix it.

## What this repo is

ZEPERION is a **LangGraph + Python package** (`zeperion/`) that orchestrates a
Planner → Developer → Reviewer → Tester loop and an optional PR delivery sub-pipeline
(commit → push → PR → Codex review → auto-merge).

A second, legacy bash implementation has been moved to the
`legacy/bash-harness` branch (kept for historical reference and the
`zeperion-init.sh` target-project template). It is **not present on `main`**
and should not be brought back here. If you need to inspect it:

```bash
git show legacy/bash-harness:.ai_longrun_harness/run_multi_agent_loop.sh
git checkout legacy/bash-harness -- .ai_longrun_harness/  # only if explicitly asked
```

There is a real test suite (`pytest`) and a real build (`pyproject.toml`,
editable install).

## Quick orientation

```
zeperion/
  agents/              # BaseAgent + AnthropicAgent + ClaudeCodeAgent + PiAgent
  graphs/
    multi_agent.py     # StateGraph assembly for Planner → Developer → Reviewer → Tester
    nodes.py           # Planner / Developer / Reviewer / Tester node implementations
    routes.py          # Multi-agent routing decisions
    control.py         # Small graph control nodes (increment/block)
    pr_pipeline/       # commit → push → PR → Codex → auto-merge (package)
      nodes.py         #   git/commit/push/PR/codex/merge/fixer node impls
      routes.py        #   decide_next_action (Codex verdict → next node)
      handoff.py       #   Planner → PR pipeline title/task handoff helpers
      graph.py         #   create_pr_pipeline_graph StateGraph assembly
  models/state.py      # TypedDict states + WorkflowConfig (Pydantic)
  parsers/section_parser.py  # Lenient TASK_ID/STATUS/LESSONS extractor
  prompts/templates/   # Jinja2 templates for planner/developer/reviewer/tester
  storage/             # File-level state + per-thread isolation
  utils/github.py      # gh CLI wrapper, Codex bot detection, paginated API
  utils/time.py        # iso_now() / utc_strftime() (timezone-aware)
  cli.py               # `zeperion init|run|status|list`

tests/                 # pytest suite (370+ tests at time of writing)
# Legacy bash version lives on the `legacy/bash-harness` branch only.
```

## How to run things

```bash
# Editable install with dev extras (anthropic + pytest + tooling)
pip install -e ".[dev]"

# Run the test suite
pytest

# Initialise a target project (creates .zeperion/config.yaml + requirement.txt)
zeperion init

# Run the multi-agent workflow
zeperion run --mode multi_agent --thread-id feature-x

# Run the PR pipeline (separate thread_id is recommended)
zeperion run --mode pr_pipeline --thread-id feature-x-pr

# Inspect status / list all threads
zeperion status --thread-id feature-x
zeperion list
```

## State storage layout

State is split across LangGraph checkpoints (SQLite) and a few JSON / text files
for human inspection. Per-thread isolation prevents parallel runs from
overwriting each other.

```
.zeperion/state/
  checkpoints.db                          # LangGraph SQLite (multi-thread)
  lessons_learned.txt                     # Cross-run lessons (shared)
  threads/<safe_thread_id>/
    workflow_state.json                   # Latest multi-agent state snapshot
    pipeline_state.json                   # Latest PR pipeline state snapshot
    planner_output.txt                    # Latest planner raw output
    developer_output.txt
    tester_output.txt
  runs/<safe_thread_id>/
    round_001_planner.txt                 # Round/fix-attempt artefacts
    round_001_developer.txt
    events.jsonl                          # Structured per-step event log
```

State ownership rules:

- `workflow_state.json` — owned by `multi_agent.py` only. Must **not** contain
  PR-pipeline fields.
- `pipeline_state.json` — owned by `pr_pipeline.py` (or the PR sub-graph node
  in `multi_agent.py`). Lives next to `workflow_state.json` for inspection.
- LangGraph SQLite checkpoints are the source of truth for resume. JSON files
  are summaries for humans and the CLI; never resume from them.

## Agent responsibilities (enforced in code)

| Role | Reads | Writes (state) | May set `GLOBAL_STATUS=DONE`? |
|------|-------|----------------|--------------------------------|
| Planner | requirement + previous plan + latest test report + lessons | `task_id`, `global_status`, `phase=DEVELOPMENT`, `lessons` | Yes |
| Developer | requirement + current plan + reviewer/tester reports + lessons | `phase=REVIEWING` (or `TESTING` when reviewer is disabled), `lessons` (no `global_status`) | **No** (silently collapsed to `CONTINUE` in `BaseAgent.parse_output`) |
| Reviewer | requirement + plan + developer output + lessons | `review_status`, `global_status`, `last_error`, `lessons` | Yes |
| Tester | requirement + plan + developer output + lessons | `test_status`, `global_status`, `last_error`, `lessons` | Yes |

`BaseAgent.parse_output` is shared by all backends; do **not** add role-specific
parsing branches in concrete agents. If you need new fields, extend the parser
and `AgentOutput` model together.

### Required vs optional output fields

`BaseAgent.parse_output` distinguishes "missing-but-tolerable" from
"missing-is-a-bug" via two role sets defined on the class:

* `_GLOBAL_STATUS_REQUIRED_ROLES = {PLANNER, REVIEWER, TESTER}`
* `_REVIEW_STATUS_REQUIRED_ROLES = {REVIEWER}`
* `_TEST_STATUS_REQUIRED_ROLES   = {TESTER}`

When a role in those sets emits output without the corresponding field
(or with an unrecognisable value), the parser populates `parse_error`
on `AgentOutput` and forces `global_status=BLOCKED`. The Planner /
Tester graph nodes then propagate `parse_error` to `state["last_error"]`
and route directly to the `blocked` terminal. **Do not** reintroduce a
silent default to `CONTINUE` here — it used to burn the entire
`max_rounds` budget on a single malformed line. Developer remains
optional because it never owns `global_status`.

### `AnthropicAgent` does not modify files

`AnthropicAgent.invoke` is a single `messages.create` call with no
tool definitions, no file IO, no shell. It returns text. That text
gets parsed and written to `*_output.txt` artifacts but **no source
code is touched**. Operators wanting the workflow to actually edit
project files must set the corresponding role's `*_agent_type` to
`pi` or `claude_code`. `PiAgent` shells out to Pi Coding Agent via
`pi --mode rpc`; `ClaudeCodeAgent` shells out to the `claude` CLI. The
CLI itself does the file writes. This used to be undocumented and was
the most common source of "I ran zeperion and nothing changed in my repo"
confusion.

## Prompt output contract (parsed by `SectionParser`)

- **Planner**: `TASK_ID:`, `GLOBAL_STATUS: CONTINUE | DONE | BLOCKED`, `PLAN:`, `RISKS:`, `HANDOFF_TO_DEVELOPER:`, `LESSONS:`
- **Developer**: `GLOBAL_STATUS: CONTINUE | BLOCKED`, `CHANGES:`, `VERIFY_HINTS:`, `BLOCKERS:`, `LESSONS:`
- **Reviewer**: `REVIEW_STATUS: PASS | FAIL | BLOCKED`, `GLOBAL_STATUS: CONTINUE | DONE | BLOCKED`, `FINDINGS:`, `FIX_REQUEST:`, `VERIFY_HINTS:`, `LESSONS:`
- **Tester**: `TEST_STATUS: PASS | FAIL | ERROR`, `GLOBAL_STATUS: CONTINUE | DONE | BLOCKED`, `TEST_CASES:`, `BUGS:`, `FIX_REQUEST:`, `LESSONS:`

Parsing is case-insensitive and tolerant of surrounding prose, but the field
names are fixed strings — changing them requires updating both the Jinja
template under `zeperion/prompts/templates/` and any assertions in `tests/`.

## Checkpointer lifecycle

Graph factories (`create_multi_agent_graph`, `create_pr_pipeline_graph`) **do
not** open SQLite connections anymore. The caller owns the lifecycle. The CLI
uses:

```python
async with AsyncSqliteSaver.from_conn_string(".zeperion/state/checkpoints.db") as saver:
    graph = create_multi_agent_graph(config, checkpointer=saver, thread_id=thread_id)
    await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}})
```

Passing `checkpointer=None` produces an in-memory graph (used by tests with
`FakeAgent`). Do not reintroduce `aiosqlite.connect(...)` inside the factories;
that was the source of the connection-leak bug fixed when the layout was
refactored.

## GitHub / Codex specifics

Encoded in `zeperion/utils/github.py`:

1. **Codex bot detection** is *not* a hard-coded `"codex"` match. `GitHubClient`
   accepts a configurable `codex_logins` set; defaults cover
   `chatgpt-codex-connector`, the `[bot]` variant, plus legacy `codex`. Any
   login ending in `[bot]` and containing `codex` also matches.
2. **`gh api --paginate` produces invalid JSON for list endpoints** — it
   concatenates per-page arrays. `_get_paginated` walks pages manually with
   `?per_page=100&page=N` instead.
3. **PR body files use `tempfile.mkstemp`** (`zeperion_pr_body_*.md`), never
   `/tmp/zeperion_pr_body.md`. Otherwise concurrent PRs would race.
4. **Auto-merge**: `gh pr merge --auto --squash --delete-branch`.

## Editing rules of thumb

- All workflow timestamps go through `zeperion.utils.time.iso_now()` (timezone-
  aware UTC ISO 8601). Do not introduce raw `datetime.utcnow()`; Python 3.12
  deprecates it.
- The `anthropic` SDK is an **optional** dependency (`pip install
  zeperion[anthropic]` or `pip install zeperion[dev]`). `zeperion.agents`
  uses lazy module-level `__getattr__` so missing `anthropic` only breaks
  `AnthropicAgent`, not `ClaudeCodeAgent`.
- Prompt templates ship inside the wheel via the `[tool.setuptools.package-data]`
  entry. The default `prompts_dir` is `None`, which makes the loader fall back
  to the packaged `zeperion/prompts/templates/`.
- When you change an output marker (`TEST_STATUS`, etc.), update both the
  Jinja template and `SectionParser` / its tests.

## What NOT to do

- Don't reintroduce PR-related fields into `WorkflowState`. They belong in
  `PRPipelineState` and `pipeline_state.json`.
- Don't read LangGraph checkpoints with `pickle.loads` — use
  `AsyncSqliteSaver.alist(None)` from the public API.
- Don't write to bare `/tmp/...` paths or hardcode source-relative directories
  like `zeperion/prompts/templates`; use `tempfile` and packaged resources.
- Don't make `Developer` set `GLOBAL_STATUS=DONE`. The parser strips that
  intentionally — adding it back in a graph node will recreate the bug.
- Don't change `BaseAgent.parse_output` to default missing
  `GLOBAL_STATUS` / `TEST_STATUS` for Planner / Tester back to
  `CONTINUE` / `PENDING`. The required-field path exists specifically
  to avoid burning a whole `max_rounds` budget on a single malformed
  response.
- Don't bring back `StateStorage.save_workflow_state` /
  `load_workflow_state`. The multi-agent graph never wrote that JSON
  file; the LangGraph SQLite checkpoint is the source of truth.
- Don't claim in docs that `AnthropicAgent` writes files. It does not.
  Use `pi` or `claude_code` for any role that needs to modify the project.
- Don't resurrect the bash harness on `main`. It lives on
  `legacy/bash-harness` and should stay there; the Python package is the
  single source of truth going forward.
