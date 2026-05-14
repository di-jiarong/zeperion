#!/usr/bin/env bash
# ==========================================================================
# State Reset Script - 重置所有状态文件到初始状态
#
# Usage:
#   bash reset_state.sh
#
# 这个脚本会：
#   1. 备份当前状态文件到 state/backups/
#   2. 重置所有 JSON 状态文件到初始状态
#   3. 清空日志文件
#   4. 保留 lessons_learned.txt（经验库）
# ==========================================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

STATE_DIR="${STATE_DIR:-./state}"
LOG_DIR="${LOG_DIR:-$STATE_DIR/logs}"
BACKUP_DIR="$STATE_DIR/backups/$(date +%Y%m%d_%H%M%S)"

echo "==> Creating backup in $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"

# Backup existing state files
for file in workflow_state.json pipeline_state.json progress.json; do
  if [[ -f "$STATE_DIR/$file" ]]; then
    cp "$STATE_DIR/$file" "$BACKUP_DIR/"
    echo "  Backed up: $file"
  fi
done

# Backup text files (except lessons_learned.txt)
for file in current_plan.txt task_result.txt test_report.txt task_queue.txt pr_body.md pr_info.txt errors.log codex_comments.txt; do
  if [[ -f "$STATE_DIR/$file" ]]; then
    cp "$STATE_DIR/$file" "$BACKUP_DIR/"
    echo "  Backed up: $file"
  fi
done

# Backup logs
if [[ -d "$LOG_DIR" ]]; then
  cp -r "$LOG_DIR" "$BACKUP_DIR/"
  echo "  Backed up: logs/"
fi

echo ""
echo "==> Resetting state files to initial state"

# Reset workflow_state.json
cat > "$STATE_DIR/workflow_state.json" <<'EOF'
{
  "status": "idle",
  "phase": "init",
  "round": 0,
  "task_id": "",
  "fix_attempt": 0,
  "owner_dev_session": "",
  "owner_test_session": "",
  "last_error": "",
  "updated_at": ""
}
EOF
echo "  ✓ workflow_state.json"

# Reset pipeline_state.json
cat > "$STATE_DIR/pipeline_state.json" <<'EOF'
{
  "status": "idle",
  "phase": "init",
  "pr_branch": "",
  "pr_target": "dev",
  "pr_number": "",
  "pr_url": "",
  "codex_status": "",
  "updated_at": ""
}
EOF
echo "  ✓ pipeline_state.json"

# Reset progress.json
cat > "$STATE_DIR/progress.json" <<'EOF'
{
  "mode": "multi_agent",
  "status": "idle",
  "updated_at": ""
}
EOF
echo "  ✓ progress.json"

# Clear work files (but keep lessons_learned.txt)
> "$STATE_DIR/current_plan.txt"
> "$STATE_DIR/task_result.txt"
> "$STATE_DIR/test_report.txt"
> "$STATE_DIR/errors.log"
echo "  ✓ Cleared work files"

# Clear PR-related files
rm -f "$STATE_DIR/pr_body.md"
rm -f "$STATE_DIR/pr_info.txt"
rm -f "$STATE_DIR/codex_comments.txt"
rm -f "$STATE_DIR/task_queue.txt"
echo "  ✓ Cleared PR and task files"

# Clear logs
if [[ -d "$LOG_DIR" ]]; then
  rm -rf "$LOG_DIR"
  mkdir -p "$LOG_DIR"
  echo "  ✓ Cleared logs"
fi

# Clear resume files
rm -f "$STATE_DIR"/*.resume_id
echo "  ✓ Cleared resume files"

echo ""
echo "==> State reset complete!"
echo ""
echo "Backup location: $BACKUP_DIR"
echo "Preserved: $STATE_DIR/lessons_learned.txt"
echo ""
echo "You can now start a fresh workflow with /zeperion"
