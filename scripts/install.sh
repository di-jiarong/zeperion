#!/usr/bin/env bash
#
# install.sh — install ZEPERION as a global `zeperion` command, the same
# way tools like `claude` are always available: an isolated environment
# with a shim on your PATH, independent of whatever conda/venv you have
# active.
#
# It uses pipx (recommended for Python CLIs). pipx puts `zeperion` on
# your global PATH and keeps its dependencies (langgraph, typer, ...) in
# a dedicated venv so they never collide with your projects.
#
# Usage:
#   scripts/install.sh [--extras "anthropic,web,github"] [--no-editable] [--force]
#
#   --extras LIST   comma-separated optional extras to include.
#                   Default: anthropic  (the Planner uses the anthropic
#                   backend out of the box and needs this).
#                   Others: web (browser UI), github (PR pipeline),
#                   tracing (OpenTelemetry), dev (tests + linters).
#   --no-editable   install a snapshot instead of an editable (-e) install.
#                   Editable (default) means `git pull` in this repo is
#                   picked up immediately with no reinstall.
#   --force         reinstall over an existing pipx install.
#
# Examples:
#   scripts/install.sh                          # editable + anthropic extra
#   scripts/install.sh --extras "anthropic,web" # also install the web UI
#   scripts/install.sh --no-editable --force    # clean snapshot reinstall
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZEPERION_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

EXTRAS="anthropic"
EDITABLE="-e"
FORCE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --extras)      EXTRAS="${2:-}"; shift 2 ;;
    --extras=*)    EXTRAS="${1#*=}"; shift ;;
    --no-editable) EDITABLE=""; shift ;;
    --force)       FORCE="--force"; shift ;;
    -h|--help)
      awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "✗ Unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Build the pip spec: a local path, optionally with [extras].
#   /path/to/repo            -> no extras
#   /path/to/repo[anthropic] -> with extras
SPEC="$ZEPERION_REPO"
if [[ -n "$EXTRAS" ]]; then
  SPEC="${ZEPERION_REPO}[${EXTRAS}]"
fi

# 1. Ensure pipx is available. Auto-install via pip --user if missing.
if ! command -v pipx >/dev/null 2>&1; then
  echo "→ pipx not found; installing it for the current user…"
  PY="$(command -v python3 || command -v python)"
  if [[ -z "$PY" ]]; then
    echo "✗ No python3/python on PATH; cannot bootstrap pipx." >&2
    echo "  Install Python 3.10+ first, or:  brew install pipx" >&2
    exit 1
  fi
  "$PY" -m pip install --user --upgrade pipx
  "$PY" -m pipx ensurepath || true
  # ensurepath edits shell rc files but not THIS shell; surface pipx now.
  if ! command -v pipx >/dev/null 2>&1; then
    PIPX="$PY -m pipx"
    echo "⚠ 'pipx' shim not on this shell's PATH yet (a new terminal will have it)."
    echo "  Falling back to: $PIPX"
  fi
fi
PIPX="${PIPX:-pipx}"

echo "→ Installing ZEPERION as a global command via pipx"
echo "  repo:     $ZEPERION_REPO"
echo "  spec:     $SPEC"
echo "  editable: $([[ -n "$EDITABLE" ]] && echo yes || echo no)"

# 2. Install. pipx forwards the spec to pip, which understands both the
#    editable flag and the path[extras] form.
# shellcheck disable=SC2086
$PIPX install $FORCE $EDITABLE "$SPEC"

# 3. Verify.
echo
if command -v zeperion >/dev/null 2>&1; then
  echo "✓ Installed: $(command -v zeperion)  ($(zeperion version 2>/dev/null || echo '?'))"
else
  echo "⚠ 'zeperion' is installed but not on THIS shell's PATH yet."
  echo "  Open a new terminal, or run:  $PIPX ensurepath  then restart the shell."
fi

cat <<EOF

✓ Done. 'zeperion' now works from any directory (like 'claude').

Next:
  # prepare a target project (creates .zeperion/config.yaml + requirement.txt):
  $ZEPERION_REPO/scripts/setup-target.sh ~/code/my-app          # pi backend
  # then:
  cd ~/code/my-app
  edit requirement.txt
  zeperion run --thread-id feature-x

Maintenance:
  pipx upgrade zeperion        # snapshot installs only; editable picks up git pulls
  pipx reinstall zeperion      # rebuild the isolated env
  pipx uninstall zeperion      # remove the global command
EOF
