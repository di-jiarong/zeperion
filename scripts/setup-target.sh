#!/usr/bin/env bash
#
# setup-target.sh — prepare any target project to be driven by ZEPERION.
#
# Usage:
#   scripts/setup-target.sh <target-project-dir> [backend] [--pi-config] [--force]
#
#   backend       pi (default) | claude_code | anthropic
#   --pi-config   also copy this repo's .pi/ (skills + guard extension)
#                 into the target so the `pi` backend has project context
#   --force       overwrite an existing .zeperion/config.yaml
#
# Examples:
#   conda activate zeperion
#   scripts/setup-target.sh ~/code/my-app                 # default: pi backend
#   scripts/setup-target.sh ~/code/my-app anthropic       # plan-only, needs API key
#   scripts/setup-target.sh ~/code/my-app pi --pi-config  # pi + copy .pi/ context
#
set -euo pipefail

# Resolve this script's repo root so --pi-config can find the source .pi/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZEPERION_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

TARGET=""
BACKEND="pi"
COPY_PI=0
FORCE=""

for arg in "$@"; do
  case "$arg" in
    --pi-config) COPY_PI=1 ;;
    --force)     FORCE="--force" ;;
    pi|claude_code|anthropic) BACKEND="$arg" ;;
    -*)          echo "✗ Unknown flag: $arg" >&2; exit 2 ;;
    *)
      if [[ -z "$TARGET" ]]; then
        TARGET="$arg"
      else
        echo "✗ Unexpected extra argument: $arg" >&2; exit 2
      fi
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 <target-project-dir> [pi|claude_code|anthropic] [--pi-config] [--force]" >&2
  exit 2
fi

# 1. zeperion CLI present?
if ! command -v zeperion >/dev/null 2>&1; then
  echo "✗ 'zeperion' CLI not found on PATH." >&2
  echo "  Activate the env that has it, e.g.:  conda activate zeperion" >&2
  echo "  Or install it:  pip install -e \"$ZEPERION_REPO\"" >&2
  exit 1
fi
echo "✓ zeperion: $(command -v zeperion)"

# 2. backend prerequisite (warn, don't hard-fail — they may install later)
case "$BACKEND" in
  pi)
    if command -v pi >/dev/null 2>&1; then
      echo "✓ pi CLI: $(command -v pi)"
    else
      echo "⚠ 'pi' CLI not on PATH — install/configure it before 'zeperion run'."
    fi
    ;;
  claude_code)
    if command -v claude >/dev/null 2>&1; then
      echo "✓ claude CLI: $(command -v claude)"
    else
      echo "⚠ 'claude' CLI not on PATH — install it before 'zeperion run'."
    fi
    ;;
  anthropic)
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
      echo "✓ ANTHROPIC_API_KEY is set"
    else
      echo "⚠ ANTHROPIC_API_KEY not set."
    fi
    echo "  NOTE: the anthropic backend is PLAN-ONLY — it produces text, it does NOT edit files."
    ;;
esac

# 3. create target dir and initialise
mkdir -p "$TARGET"
TARGET_ABS="$(cd "$TARGET" && pwd)"
echo "→ Initialising ZEPERION in: $TARGET_ABS (backend=$BACKEND)"
( cd "$TARGET_ABS" && zeperion init --backend "$BACKEND" $FORCE )

# 4. optional: copy .pi/ project context for the pi backend
if [[ "$COPY_PI" -eq 1 ]]; then
  if [[ -d "$ZEPERION_REPO/.pi" ]]; then
    echo "→ Copying .pi/ (skills + guard extension) into target"
    cp -R "$ZEPERION_REPO/.pi" "$TARGET_ABS/.pi"
    echo "✓ Copied $ZEPERION_REPO/.pi → $TARGET_ABS/.pi"
  else
    echo "⚠ --pi-config requested but $ZEPERION_REPO/.pi not found; skipping."
  fi
fi

cat <<EOF

✓ Ready. Next steps:
  1) cd "$TARGET_ABS"
  2) edit requirement.txt   (describe what you want built)
  3) zeperion run --thread-id feature-x
     # watch:   zeperion status -t feature-x --watch
     # logs:    zeperion logs   -t feature-x --follow
     # web UI:  zeperion serve   (needs the [web] extra)

  Tweak .zeperion/config.yaml as needed, e.g.:
    max_total_tokens: 200000     # hard token budget (0 = unlimited)
    tester_verify_commands:      # real commands the Tester runs before judging
      - pytest -q
EOF
