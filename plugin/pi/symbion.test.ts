// The extension's helpers, run by `node --test` (tests/test_pi.py). The
// handlers themselves are checked live in Pi.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import extension, { SETUP_PREFACE, WRAP_PREFACE, commandPrompt, gotchaInput, gotchaText, hookMessage,
  run } from "./symbion.ts";

const HOOK = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"A"}';

test("hookMessage parses the hook's JSON", () => {
  assert.deepEqual(hookMessage(`${HOOK},"systemMessage":"L"}\n`), { content: "A", notify: "L" });
  assert.deepEqual(hookMessage(`${HOOK}}`), { content: "A", notify: null });
});

test("hookMessage passes plain text through", () => {
  assert.deepEqual(hookMessage("Traceback (most recent call last):\n  boom\n"),
                   { content: "Traceback (most recent call last):\n  boom", notify: null });
  assert.equal(hookMessage(""), null);
  assert.equal(hookMessage(" \n"), null);
});

test("gotchaInput renames path for edit and write only", () => {
  const edit = JSON.parse(gotchaInput("s1", "edit", { path: "src/x.py", edits: [] }, false)!);
  assert.equal(edit.session_id, "s1");
  assert.equal(edit.hook_event_name, "PostToolUse");
  assert.equal(edit.tool_name, "Edit");
  assert.deepEqual(edit.tool_input, { file_path: "src/x.py", edits: [] });
  const write = JSON.parse(gotchaInput("s1", "write", { path: "a b.md", content: "x" }, false)!);
  assert.equal(write.tool_name, "Write");
  assert.deepEqual(write.tool_input, { file_path: "a b.md", content: "x" });
  const read = JSON.parse(gotchaInput("s1", "read", { path: "src/x.py" }, true)!);
  assert.equal(read.tool_name, "Read");
  assert.equal(read.hook_event_name, "PostToolUseFailure");
  assert.deepEqual(read.tool_input, { path: "src/x.py" });
  assert.equal(JSON.parse(gotchaInput("s1", "bash", { command: "cat x" }, false)!).tool_name, "Bash");
  for (const tool of ["grep", "find", "ls", "powershell"]) {
    assert.equal(gotchaInput("s1", tool, { path: "x" }, false), null, tool);
  }
});

test("gotchaText ignores output without additionalContext", () => {
  assert.equal(gotchaText(`${HOOK}}\n`), "A");
  for (const out of ["", "garbage", "{}", "PKG-GOTCHAS\n"]) assert.equal(gotchaText(out), null, out);
});

test("commandPrompt strips the frontmatter", () => {
  const md = readFileSync(new URL("../skills/setup/SKILL.md", import.meta.url), "utf8");
  const said = commandPrompt(md, "PREFACE", "");
  assert.ok(said.startsWith("PREFACE\n\n"), said.slice(0, 40));
  assert.ok(!said.includes("name: setup"));
  assert.ok(said.includes("symbion init"));
  assert.ok(commandPrompt(md, "PREFACE", "in a call of its own").endsWith("\n\nin a call of its own"));
});

test("run resolves empty on timeout", async () => {
  const t = Date.now();
  assert.equal(await run("sh", ["-c", "sleep 5"], "", ".", process.env, 200), "");
  assert.ok(Date.now() - t < 2000);
});

test("run resolves empty when the program is missing", async () => {
  assert.equal(await run("no-such-program-xyz", [], "", ".", process.env, 1000), "");
});

test("run hands its input to stdin", async () => {
  assert.equal(await run("sh", ["-c", "cat"], "abc", ".", process.env, 2000), "abc");
});

test("the prefaces name no command the model cannot run", () => {
  // `/skill:symbion` and `symbion:symbion` are typed by the user; a model ran the
  // second in the shell (live check, 2026-11-03). Pi's model reads a skill's file.
  for (const preface of [SETUP_PREFACE, WRAP_PREFACE]) {
    assert.ok(!preface.includes("/skill:"), preface);
  }
  assert.match(WRAP_PREFACE, /read the symbion skill's SKILL\.md with the read tool/);
});

test("the setup preface keeps a CLAUDE.md that Pi reads", () => {
  // Pi reads the first of AGENTS.md and CLAUDE.md in a directory: a new AGENTS.md
  // beside a CLAUDE.md hides it from every later session.
  assert.match(SETUP_PREFACE, /CLAUDE\.md/);
  assert.match(SETUP_PREFACE, /AGENTS\.md only when neither/);
});

test("a command typed mid-run queues its prompt as a follow-up", async () => {
  // Pi runs a command at once, even mid-run, where a bare sendUserMessage throws.
  const commands = new Map();
  const sent: unknown[] = [];
  const pi = { on() {}, registerCommand: (name: string, o: unknown) => commands.set(name, o),
               sendUserMessage: (_text: string, options?: unknown) => sent.push(options) };
  extension(pi as any);
  for (const name of ["symbion-setup", "symbion-wrap"]) {
    await commands.get(name).handler("", { isIdle: () => false });
    await commands.get(name).handler("", { isIdle: () => true });
  }
  assert.deepEqual(sent, [{ deliverAs: "followUp" }, undefined, { deliverAs: "followUp" }, undefined]);
});
