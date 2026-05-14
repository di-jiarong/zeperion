#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

if [[ -f "./config.env" ]]; then
  # shellcheck disable=SC1091
  source "./config.env"
elif [[ -f "./config.env.example" ]]; then
  # shellcheck disable=SC1091
  source "./config.env.example"
fi

REQUIREMENT_FILE="${REQUIREMENT_FILE:-./requirement_template.txt}"
STATE_DIR="${STATE_DIR:-./state}"
PROMPT_DIR="${PROMPT_DIR:-./prompts}"
LOG_DIR="${LOG_DIR:-./state/logs}"
TASK_QUEUE_FILE="${STATE_DIR}/task_queue.txt"
LESSONS_FILE="${STATE_DIR}/lessons_learned.txt"
PROGRESS_FILE="${STATE_DIR}/progress.json"

MODEL="${SINGLE_MODEL:-fast-model}"
MAX_CYCLES="${SINGLE_MAX_CYCLES:-200}"
SLEEP_SECONDS="${SINGLE_SLEEP_SECONDS:-2}"
SINGLE_AGENT_CALL_TEMPLATE="${SINGLE_AGENT_CALL_TEMPLATE:-}"

mkdir -p "$STATE_DIR" "$PROMPT_DIR" "$LOG_DIR"
touch "$TASK_QUEUE_FILE" "$LESSONS_FILE"

if [[ ! -f "$REQUIREMENT_FILE" ]]; then
  echo "缺少需求文件: $REQUIREMENT_FILE"
  exit 1
fi

if [[ -z "$SINGLE_AGENT_CALL_TEMPLATE" ]]; then
  echo "未配置 SINGLE_AGENT_CALL_TEMPLATE，请先编辑 config.env"
  exit 1
fi

now_iso() {
  date -Iseconds
}

# 安全地提取标记段落（带停止条件）
extract_section() {
  local file="$1"
  local start_marker="$2"
  local stop_markers="$3"  # 空格分隔的停止标记列表

  if [[ ! -f "$file" ]]; then
    echo ""
    return 1
  fi

  awk -v start="$start_marker" -v stops="$stop_markers" '
    BEGIN {
      flag=0
      split(stops, stop_array, " ")
    }
    $0 ~ "^" start {
      flag=1
      next
    }
    flag {
      for (i in stop_array) {
        if ($0 ~ "^" stop_array[i]) {
          exit
        }
      }
      if (NF == 0 && !printed_any) next
      print
      printed_any=1
    }
  ' "$file"
}

write_progress() {
  local status="$1"
  jq -n \
    --arg mode "single_agent_ralph" \
    --arg status "$status" \
    --arg updated_at "$(now_iso)" \
    '{mode: $mode, status: $status, updated_at: $updated_at}' \
    > "$PROGRESS_FILE"
}

pending_count() {
  awk 'NF{count++} END{print count+0}' "$TASK_QUEUE_FILE"
}

pop_first_task() {
  local first
  first="$(awk 'NF{print; exit}' "$TASK_QUEUE_FILE")"
  if [[ -z "$first" ]]; then
    echo ""
    return
  fi
  awk 'BEGIN{done=0} {if(!done && NF){done=1; next} print}' "$TASK_QUEUE_FILE" > "${TASK_QUEUE_FILE}.tmp"
  mv "${TASK_QUEUE_FILE}.tmp" "$TASK_QUEUE_FILE"
  echo "$first"
}

append_task() {
  local task="$1"
  if [[ -n "$task" ]]; then
    echo "$task" >> "$TASK_QUEUE_FILE"
  fi
}

run_agent_once() {
  local prompt_file="$1"
  local output_file="$2"
  local log_file="$3"

  # 优先使用新的结构化配置（安全）
  if [[ -n "${LLM_CLI_COMMAND:-}" ]]; then
    local cmd=("$LLM_CLI_COMMAND")

    # 添加固定参数
    if [[ -n "${LLM_CLI_ARGS:-}" ]]; then
      read -ra args <<< "$LLM_CLI_ARGS"
      cmd+=("${args[@]}")
    fi

    # 添加 model 参数
    if [[ "${LLM_SUPPORT_MODEL_FLAG:-true}" == "true" ]]; then
      cmd+=(--model "$MODEL")
    fi

    # 执行命令
    if [[ "${LLM_USE_STDIN:-true}" == "true" ]]; then
      "${cmd[@]}" < "$prompt_file" > "$output_file" 2>> "$log_file"
    else
      "${cmd[@]}" --prompt-file "$prompt_file" > "$output_file" 2>> "$log_file"
    fi
  # 回退到旧的 eval 模式（兼容性，但有安全风险）
  elif [[ -n "${SINGLE_AGENT_CALL_TEMPLATE:-}" ]]; then
    export MODEL PROMPT_FILE OUTPUT_FILE LOG_FILE
    PROMPT_FILE="$prompt_file"
    OUTPUT_FILE="$output_file"
    LOG_FILE="$log_file"
    eval "$SINGLE_AGENT_CALL_TEMPLATE"
  else
    echo "错误：未配置 LLM_CLI_COMMAND 或 SINGLE_AGENT_CALL_TEMPLATE" >&2
    return 1
  fi
}

cycle=1
write_progress "running"

while (( cycle <= MAX_CYCLES )); do
  todo="$(pop_first_task)"
  if [[ -z "$todo" ]]; then
    write_progress "completed"
    echo "全部任务完成。"
    exit 0
  fi

  prompt_file="${STATE_DIR}/single_prompt.txt"
  output_file="${STATE_DIR}/single_output_cycle_${cycle}.txt"
  log_file="${LOG_DIR}/single_agent.log"

  cat > "$prompt_file" <<EOF
你是长任务执行智能体。请严格执行下面任务，并按指定格式输出。

【需求文件】
$REQUIREMENT_FILE

【当前任务】
$todo

【经验库】
$LESSONS_FILE

【输出要求】
请输出以下四段：
1) RESULT: 本次完成结果
2) NEW_TASKS: 若有后续子任务，每行一个，以 "- " 开头；没有就写 "NONE"
3) ISSUES: 遇到的问题
4) LESSONS: 新增经验（可为空）
EOF

  run_agent_once "$prompt_file" "$output_file" "$log_file" || true

  if [[ ! -s "$output_file" ]]; then
    echo "第 $cycle 轮输出为空，任务重新入队: $todo" | tee -a "$log_file"
    append_task "$todo"
    ((cycle++))
    write_progress "running"
    sleep "$SLEEP_SECONDS"
    continue
  fi

  # 安全提取 LESSONS（带停止条件）
  extract_section "$output_file" "LESSONS:" "RESULT: NEW_TASKS: ISSUES:" >> "$LESSONS_FILE"

  # 安全提取 NEW_TASKS（在 ISSUES 之前停止）
  new_tasks="$(extract_section "$output_file" "NEW_TASKS:" "ISSUES: LESSONS: RESULT:" | sed 's/^- //g' | awk 'NF')"
  if [[ -n "$new_tasks" ]] && ! grep -qx "NONE" <<< "$new_tasks"; then
    while IFS= read -r t; do
      append_task "$t"
    done <<< "$new_tasks"
  fi

  ((cycle++))
  write_progress "running"
  sleep "$SLEEP_SECONDS"
done

write_progress "stopped_max_cycles"
echo "达到最大循环次数: $MAX_CYCLES，已停止。"
exit 0
