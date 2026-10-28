# `/symbion wrap`: the session end

A wrap is the session's last use of its context: what it knows that no entry
holds goes into the store now. It leaves the store true and one commit whose
message is the handoff, and writes no file. The next session's summary prints
that message as `last wrap by …`, with its age and the repo commits since,
above the open entries.

1. **Asks.** Reread your own messages in this session. Each ask you put to
   the user ("say if…", "want me to…?", "should I…?") that no later message
   of theirs answered is an open `question`: the ask in plain words, and when
   its answers fit in three, `--option` for each, the default first. An ask held only in chat, a note, a
   resolved entry or the handoff is not an open `question`. Where
   `symbion schema` lists no `question` kind, the handoff's paragraph names
   each such ask instead, with what the work does until an answer.

2. **Entries.** What the session learned, decided, found broken or started,
   and no entry holds yet, goes on the object it concerns, after a
   `list --grep` for it:
   - a trap on its file, tagged `gotcha`; a decision; a bug; a task, due when
     a job is still running.

   The entries the session touched get tied off:
   - each one it finished and left open: `resolve --body`, saying how;
   - each one whose truth changed: `supersede`;
   - each gotcha the hook printed this session that no longer holds:
     `supersede <id> --add-tag retired`.

   A practice for one file is its `gotcha`. A practice about the harness or
   the user stays in your own memory. A practice for the whole repo (a check
   before each commit, a tool to prefer) is a `task` for the user, tagged
   `practice`, on the instruction file it belongs in, or on `project` when
   `add` refuses the file, only when it names a failure this session hit that
   the practice would have prevented. The wrap never edits an instruction
   file. A number git or symbion prints goes nowhere. State nothing prints
   (what a test host holds, which release is public) is a plain entry on its
   item.

3. **Commit.** `symbion commit --dry-run`, in a call of its own, shows what
   goes in, and `symbion list --since wrap --limit 0` what was written since
   the last wrap, each entry with its state and result. Then:

   ```bash
   symbion commit --wrap -m - <<'EOF'
   <one line: what to pick up first>

   <one paragraph, about 500 characters: what the session did and why, what
   it left open, and the few entries the next session reads first, by their
   10-character tails>
   EOF
   ```

   The next summary lists first the entries that wait on its reader: someone
   else wrote their newest word. `symbion summary` shows them now, and the
   first line names what comes after them. The summary lists the open entries,
   so the paragraph names what was left open in a clause. A why in it names
   what showed it, as in an entry. An outcome or a count in it comes from
   that list, in its words: "mostly held" stays "mostly held". `--wrap`
   commits the message even when no entry changed. The heredoc goes to `-m -`,
   never inside `-m "$( )"`, where bash 3.2 breaks on an apostrophe. The wrap
   does not push unless the project's instructions say so, or the store is
   shared: there `commit` pulls, then pushes.

4. **Report**, in a few lines: the entries added, superseded and resolved; the
   practices filed; the commit.

A continuity tool's handoff file (`NEXT_SESSION.md`) is that tool's: the open
list stays out of it.
