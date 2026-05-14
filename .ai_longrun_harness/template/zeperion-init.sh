#!/usr/bin/env bash
# ==========================================================================
# ZEPERION 初始化脚本 — 在新项目中一键部署开发交付管线
#
# 用法:
#   cd /path/to/new/project
#   bash /path/to/zeperion-init.sh
#
# 效果:
#   在当前项目创建 .claude/commands/、CLAUDE.md、AGENTS.md
#   （.ai_longrun_harness/ 管线脚本需要从源项目手动复制）
# ==========================================================================

set -euo pipefail

# 此脚本所在目录（同时存放了所有模板文件）
DIR="$(cd "$(dirname "$0")" && pwd)"
DST="$(pwd)"

echo "Initializing ZEPERION in: $DST"

# ── .claude/settings.json ──────────────────────────────────────────

if [[ -f "$DST/.claude/settings.json" ]]; then
  echo "  .claude/settings.json 已存在，跳过。"
else
  mkdir -p "$DST/.claude"
  cp "$DIR/settings.json" "$DST/.claude/"
  echo "  .claude/settings.json 已创建"
fi

# ── .claude/settings.local.json ────────────────────────────────────

if [[ -f "$DST/.claude/settings.local.json" ]]; then
  echo "  .claude/settings.local.json 已存在，跳过。"
else
  mkdir -p "$DST/.claude"
  cp "$DIR/.settings.local.json" "$DST/.claude/settings.local.json"
  echo "  .claude/settings.local.json 已创建（权限预配置）"
fi

# ── .claude/commands/ ─────────────────────────────────────────────

mkdir -p "$DST/.claude/commands"
for cmd in zeperion.md zeperion-pr.md; do
  if [[ -f "$DST/.claude/commands/$cmd" ]]; then
    echo "  .claude/commands/$cmd 已存在，跳过。"
  else
    cp "$DIR/$cmd" "$DST/.claude/commands/"
    echo "  .claude/commands/$cmd 已创建"
  fi
done

# ── AGENTS.md ──────────────────────────────────────────────────────

if [[ -f "$DST/AGENTS.md" ]]; then
  echo "  AGENTS.md 已存在，跳过。"
else
  cp "$DIR/AGENTS.md" "$DST/"
  echo "  AGENTS.md 已创建"
fi

# ── CLAUDE.md ──────────────────────────────────────────────────────

if [[ -f "$DST/CLAUDE.md" ]]; then
  echo "  CLAUDE.md 已存在，跳过。"
else
  cp "$DIR/CLAUDE.md" "$DST/"
  echo "  CLAUDE.md 已创建"
fi

# ── .gitignore ─────────────────────────────────────────────────────

if [[ -f "$DST/.gitignore" ]]; then
  # 检查是否已经包含 .ai_longrun_harness
  if grep -q "^\.ai_longrun_harness/" "$DST/.gitignore" 2>/dev/null; then
    echo "  .gitignore 已包含 .ai_longrun_harness/，跳过。"
  else
    echo "" >> "$DST/.gitignore"
    echo "# ZEPERION workflow directory" >> "$DST/.gitignore"
    echo ".ai_longrun_harness/" >> "$DST/.gitignore"
    echo "  .gitignore 已更新（添加 .ai_longrun_harness/）"
  fi

  # 检查是否已经包含 .claude
  if grep -q "^\.claude" "$DST/.gitignore" 2>/dev/null; then
    echo "  .gitignore 已包含 .claude，跳过。"
  else
    echo ".claude" >> "$DST/.gitignore"
    echo "  .gitignore 已更新（添加 .claude）"
  fi
else
  cat > "$DST/.gitignore" <<'EOF'
# ZEPERION workflow directory
.ai_longrun_harness/
.claude
EOF
  echo "  .gitignore 已创建"
fi

echo ""
echo "Done. 新增文件："
echo "  .claude/settings.json"
echo "  .claude/settings.local.json (权限预配置，无需手动同意)"
echo "  .claude/commands/zeperion.md"
echo "  .claude/commands/zeperion-pr.md"
echo "  AGENTS.md"
echo "  CLAUDE.md"
echo "  .gitignore (已配置忽略 .ai_longrun_harness/ 和 .claude)"
echo ""
echo ".ai_longrun_harness/ 管线脚本请从源项目复制:"
echo "  cp -r <源项目>/.ai_longrun_harness ./"
