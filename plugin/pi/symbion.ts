// The symbion extension for Pi. It gives Pi what the plugin's hooks give Claude
// Code by running the same shims (plugin/hooks/) and the installed CLI's files:
// the package carries no copy of either (spec 2026-11-02). It never fails a
// session: a failure, a timeout or a missing `sh` sends nothing.
import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const PLUGIN = fileURLToPath(new URL("..", import.meta.url));
const START = join(PLUGIN, "hooks", "session_start.sh");
const GOTCHAS = join(PLUGIN, "hooks", "gotchas.sh");
// Claude Code's names for the tools its gotcha matcher covers. Pi's grep, find
// and ls stay out, as Claude Code's Grep and Glob do.
const TOOLS = new Map([["read", "Read"], ["edit", "Edit"], ["write", "Write"], ["bash", "Bash"]]);

export const SETUP_PREFACE = "You are in Pi. Here this command is /symbion-setup. Wherever the steps " +
  "below run `symbion init`, add `--agent pi`. Pi reads one instructions file per directory, " +
  "the first of AGENTS.md and CLAUDE.md it finds: add the line to whichever exists, and create " +
  "AGENTS.md only when neither does. Pi expands no `!` line: run each one yourself and use its " +
  "output. Once the CLI is installed, tell the user to run /reload so Pi loads the symbion skill.";
// A Pi model loads a skill by reading its file; `/skill:` commands are the user's.
export const WRAP_PREFACE = "You are in Pi. Where the steps below say to load `symbion:symbion`, " +
  "read the symbion skill's SKILL.md with the read tool: your skills list gives its path. Pi " +
  "expands no `!` line: run each one yourself and use its output.";

/** `file` with `input` on stdin; its stdout, or "" on any error, timeout or nonzero exit. */
export function run(file: string, args: string[], input: string, cwd: string,
                    env: NodeJS.ProcessEnv, timeoutMs: number): Promise<string> {
  return new Promise((resolve) => {
    try {
      const child = execFile(file, args, { cwd, env, timeout: timeoutMs },
                             (err, stdout) => resolve(err ? "" : String(stdout)));
      child.stdin?.on("error", () => {});   // a child that exits before it reads
      child.stdin?.end(input);
    } catch {
      resolve("");
    }
  });
}

/** The start hook's answer: Claude Code's JSON, or plain text (an error, the
 * not-installed line), which reaches the model as it is. */
export function hookMessage(out: string): { content: string; notify: string | null } | null {
  const text = out.trim();
  if (!text) return null;
  try {
    const j = JSON.parse(text);
    const content = j?.hookSpecificOutput?.additionalContext;
    if (typeof content === "string") {
      return { content, notify: typeof j.systemMessage === "string" ? j.systemMessage : null };
    }
  } catch {}
  return { content: text, notify: null };
}

/** Claude Code's PostToolUse JSON for gotchas.sh, or null for a tool it skips. */
export function gotchaInput(sid: string, toolName: string, input: unknown,
                            isError: boolean): string | null {
  const tool = TOOLS.get(toolName);
  if (!tool) return null;
  let args = input;
  if ((tool === "Edit" || tool === "Write") && input && typeof input === "object") {
    // gotchas.sh reads an edit's file from its first `*_path` key; Pi names it `path`.
    const { path, ...rest } = input as Record<string, unknown>;
    args = { file_path: path, ...rest };
  }
  return JSON.stringify({ session_id: sid, hook_event_name: isError ? "PostToolUseFailure" : "PostToolUse",
                          tool_name: tool, tool_input: args });
}

/** The gotcha lines in gotchas.sh's answer, or null. */
export function gotchaText(out: string): string | null {
  try {
    const text = JSON.parse(out)?.hookSpecificOutput?.additionalContext;
    return typeof text === "string" && text ? text : null;
  } catch {
    return null;
  }
}

/** A plugin skill's text as a Pi prompt: the preface, the body without its
 * frontmatter, then the command's arguments. */
export function commandPrompt(skillMd: string, preface: string, args: string): string {
  const body = skillMd.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").trim();
  return [preface, body, args.trim()].filter(Boolean).join("\n\n");
}

export default function (pi: ExtensionAPI) {
  const deliver = async (ctx: ExtensionContext) => {
    try {
      const out = await run("sh", [START, "pi"],
                            JSON.stringify({ session_id: ctx.sessionManager.getSessionId() }),
                            ctx.cwd, { ...process.env, SYMBION_PROJECT_DIR: ctx.cwd }, 10_000);
      const m = hookMessage(out);
      if (!m) return;
      // Steered: an idle session stores it for the next prompt, and a run that
      // compacted mid-way gets it before its next model request.
      pi.sendMessage({ customType: "symbion", content: m.content, display: false },
                     { deliverAs: "steer" });
      if (m.notify) ctx.ui.notify(m.notify, "info");
    } catch {}
  };
  pi.on("session_start", async (event, ctx) => {
    if (event.reason !== "reload") await deliver(ctx);   // a reload keeps what the session has
  });
  pi.on("session_compact", async (_event, ctx) => deliver(ctx));

  pi.on("resources_discover", async (event) => {
    try {
      const r = await pi.exec("symbion", ["skill", "--path"], { cwd: event.cwd, timeout: 5_000 });
      const skill = join(r.stdout.trim(), "SKILL.md");
      if (r.code === 0 && existsSync(skill)) return { skillPaths: [skill] };
    } catch {}
    return {};
  });

  pi.on("tool_result", async (event, ctx) => {
    try {
      const input = gotchaInput(ctx.sessionManager.getSessionId(), event.toolName, event.input,
                                event.isError);
      if (!input) return;
      const text = gotchaText(await run("sh", [GOTCHAS], input, ctx.cwd, process.env, 5_000));
      if (text) {
        return { content: [...event.content, { type: "text", text }],
                 structuredContent: event.structuredContent };
      }
    } catch {}
  });

  for (const [name, skill, preface] of [["symbion-setup", "setup", SETUP_PREFACE],
                                         ["symbion-wrap", "wrap", WRAP_PREFACE]]) {
    const md = join(PLUGIN, "skills", skill, "SKILL.md");
    let description = `symbion ${skill}`;
    try {
      description = readFileSync(md, "utf8").match(/^description: "?(.*?)"?\r?$/m)?.[1] ?? description;
    } catch {}
    pi.registerCommand(name, {
      description,
      handler: async (args, ctx) => {
        try {
          // Pi runs a command at once, even mid-run, where a bare sendUserMessage throws.
          pi.sendUserMessage(commandPrompt(readFileSync(md, "utf8"), preface, args),
                             ctx.isIdle() ? undefined : { deliverAs: "followUp" });
        } catch {}
      },
    });
  }
}
