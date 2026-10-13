# `/symbion wrap`: the session end

Run this when the person asks for a wrap (`/symbion wrap`), and whenever any
tool's wrap runs. It leaves rows and one store commit. It writes no file: the
commit's message is the handoff, and the next session's `symbion summary`
prints it as `last wrap by …`, with its age and the repo commits since.

1. **The end-of-day question.** What did this session learn, decide, find
   broken, start or ask, that no row holds yet? Search before you write:
   `symbion list --grep 'a|b'`. Then, on the object each one concerns:
   - `symbion add` a trap on its file (`--tag gotcha`), a decision, a bug, or
     a task (`--due` when a job is still running).
   - `symbion supersede <id>` each row the session changed the truth of.
   - `symbion resolve <id> --body …` each row the session finished, saying how.
   - A question you asked the person that has no answer yet is a `question`
     row, with the default the work holds until it comes. In the message
     alone, the answer has no row to land on.
   - A practice about one file (a step that must come before an edit) is that
     file's `gotcha`, and nothing more: the hook gives it to a session the
     first time it touches the file. A practice about the whole project (a
     check to run before a commit, a tool to prefer), one you hold only in
     your own memory included, is a `task` for the person, on the
     instruction file it belongs in, tagged `practice`: `symbion add task
     --target file:CLAUDE.md --tag practice --body "For the person, to move
     into this file: …"`, or `--target project` when `symbion schema` lists
     no `file` type. Every session start lists it until they move it and resolve it.
     The wrap never edits an instruction file.

   A number git or symbion prints (open, unpushed, behind) goes nowhere. State
   nothing prints (what a test host holds, which release is public) is a plain
   row on its item, superseded when it changes.

2. **Commit the store, with the day as its message.** `symbion commit
   --dry-run` first, in a call of its own, to see what goes in. Then:

   ```bash
   symbion commit --wrap -m "$(cat <<'EOF'
   <one line: what to pick up first>

   <one paragraph: what the session did and why, what it left open, the
   rows that matter by their 10-character tails>
   EOF
   )"
   ```

   The first line is what the next session reads first. The open rows stay
   out of the message: the summary lists them. The quoted heredoc keeps
   backticks and `$` literal. `--wrap` refuses to commit without `-m`. When
   the dry run reads `nothing to commit`, wrap anyway: `--wrap` then commits
   the message alone. The wrap does not push unless the project's
   instructions say to.

3. **Report**, in a few lines: the rows added, superseded and resolved; the
   practices filed for the person; the commit.

A session-continuity tool's handoff file (`NEXT_SESSION.md`) is that tool's:
leave it to that tool, and never write the open list into it.
