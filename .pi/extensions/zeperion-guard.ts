import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  isBashToolResult,
  isToolCallEventType,
} from "@earendil-works/pi-coding-agent";

const PROTECTED_PATH_PATTERNS = [
  /(^|\/)\.env(\.|$)/,
  /(^|\/)\.zeperion\/state(\/|$)/,
  /(^|\/)\.zeperion\/logs(\/|$)/,
  /(^|\/)node_modules(\/|$)/,
  /(^|\/)\.git(\/|$)/,
];

const DESTRUCTIVE_COMMAND_PATTERNS = [
  /\brm\s+-[^&|;\n]*r[f]?\b/,
  /\bgit\s+reset\s+--hard\b/,
  /\bgit\s+clean\s+-[^&|;\n]*[fdx]/,
  /\bchmod\s+-R\b/,
  /\bchown\s+-R\b/,
  /\bsudo\b/,
];

const MUTATING_COMMAND_PATTERNS = [
  /\bgit\s+commit\b/,
  /\bgit\s+merge\b/,
  /\bgit\s+rebase\b/,
  /\bgit\s+stash\s+(pop|apply)\b/,
  /\bpython\s+.*\s+-m\s+pip\s+install\b/,
  /\bpip\s+install\b/,
  /\bnpm\s+(install|update|audit\s+fix)\b/,
  /\bpnpm\s+(install|update)\b/,
  /\byarn\s+(add|install|upgrade)\b/,
];

const TEST_COMMAND_PATTERNS = [
  /\bpytest\b/,
  /\bnpm\s+(test|run\s+test)\b/,
  /\bpnpm\s+(test|run\s+test)\b/,
  /\byarn\s+(test|run\s+test)\b/,
  /\btox\b/,
  /\bruff\s+check\b/,
  /\bmypy\b/,
];

let checkpointBranch: string | undefined;

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    ctx.ui.setStatus("zeperion", "ZEPERION guard active");
  });

  pi.on("before_agent_start", async (event) => {
    const reminder = [
      "",
      "ZEPERION guard reminder:",
      "- Keep Planner/Developer/Reviewer/Tester output markers intact.",
      "- Reviewer is a review gate; Tester owns command-level verification.",
      "- Do not stage .zeperion/state or .zeperion/logs.",
    ].join("\n");

    return {
      systemPrompt: `${event.systemPrompt}\n${reminder}`,
    };
  });

  pi.on("tool_call", async (event, ctx) => {
    if (isToolCallEventType("bash", event)) {
      const command = event.input.command ?? "";
      if (
        DESTRUCTIVE_COMMAND_PATTERNS.some((pattern) => pattern.test(command)) ||
        MUTATING_COMMAND_PATTERNS.some((pattern) => pattern.test(command))
      ) {
        await ensureCheckpoint(pi, ctx, "before-mutating-command");
      }

      if (DESTRUCTIVE_COMMAND_PATTERNS.some((pattern) => pattern.test(command))) {
        const ok = await ctx.ui.confirm(
          "Potentially destructive command",
          `Allow this command?\n\n${command}`,
        );
        if (!ok) {
          return { block: true, reason: "Blocked by ZEPERION guard" };
        }
      }
    }

    if (isToolCallEventType("write", event) || isToolCallEventType("edit", event)) {
      const target = String(event.input.path ?? event.input.file_path ?? "");
      if (PROTECTED_PATH_PATTERNS.some((pattern) => pattern.test(target))) {
        return {
          block: true,
          reason: `ZEPERION guard blocks writes to protected path: ${target}`,
        };
      }
      await ensureCheckpoint(pi, ctx, "before-file-edit");
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    if (!isBashToolResult(event)) return;

    const command = String(event.input?.command ?? "");
    const details = event.details as { exitCode?: number; code?: number } | undefined;
    const exitCode = details?.exitCode ?? details?.code;
    const failed = event.isError || (typeof exitCode === "number" && exitCode !== 0);
    const isTestCommand = TEST_COMMAND_PATTERNS.some((pattern) => pattern.test(command));

    if (!isTestCommand || !failed) return;

    const feedback = [
      "ZEPERION guard detected a failed verification command.",
      "",
      `Command: ${command}`,
      `Exit code: ${exitCode ?? "unknown"}`,
      "",
      "Please inspect the failure, fix the smallest relevant issue, and rerun verification before reporting TEST_STATUS: PASS.",
    ].join("\n");

    ctx.ui.notify("Verification failed; queued ZEPERION feedback", "warning");
    pi.sendUserMessage(feedback, { deliverAs: "followUp" });

    return {
      content: [
        ...event.content,
        { type: "text", text: `\n\n${feedback}\n` },
      ],
      isError: true,
    };
  });

  pi.registerCommand("zeperion-checkpoint", {
    description: "Create a git checkpoint branch before risky edits",
    handler: async (args, ctx) => {
      const reason = (args || "manual").trim().replace(/[^A-Za-z0-9_.-]+/g, "-");
      const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
      const branch = `codex/checkpoint-${stamp}-${reason}`.slice(0, 80);
      const result = await pi.exec("git", ["branch", branch], { cwd: ctx.cwd });
      if (result.code === 0) {
        ctx.ui.notify(`Checkpoint created: ${branch}`, "info");
      } else {
        ctx.ui.notify(`Checkpoint failed: ${result.stderr || result.stdout}`, "error");
      }
    },
  });
}

async function ensureCheckpoint(
  pi: ExtensionAPI,
  ctx: { cwd: string; ui: { notify(message: string, level?: string): void } },
  reason: string,
) {
  if (checkpointBranch) return checkpointBranch;

  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const branch = `codex/checkpoint-${stamp}-${reason}`.slice(0, 80);
  const result = await pi.exec("git", ["branch", branch], { cwd: ctx.cwd });
  if (result.code === 0) {
    checkpointBranch = branch;
    ctx.ui.notify(`ZEPERION checkpoint created: ${branch}`, "info");
    return branch;
  }

  ctx.ui.notify(`ZEPERION checkpoint failed: ${result.stderr || result.stdout}`, "warning");
  return undefined;
}
