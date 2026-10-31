"""The diff viewer's parser and drawing (gui/diffview.py), and the git
answers it reads (gitref.commit_show, commit_files, since_base, diff_text)."""
import subprocess

from symbion import gitref
from symbion.gui import diffview as D

TWO_HUNKS = """\
diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -1,4 +1,4 @@ def f():
 a = 1
-b = 2
+b = 3
 c = 3
 d = 4
@@ -10,3 +10,4 @@ def g():
 x
+y
 z
 w
"""


def test_line_numbers_follow_each_hunk():
    (f,) = D.parse(TWO_HUNKS)
    assert (f.name, f.plus, f.minus) == ("x.py", 2, 1)
    (h1, l1), (h2, l2) = f.hunks
    assert h1 == "@@ -1,4 +1,4 @@ def f():"
    assert l1[1] == ("-", 2, None, "b = 2") and l1[2] == ("+", None, 2, "b = 3")
    assert l1[3] == (" ", 3, 3, "c = 3")
    assert l2[1] == ("+", None, 11, "y") and l2[2] == (" ", 11, 12, "z")


def test_a_deleted_line_that_reads_like_a_header_stays_in_its_hunk():
    """A hunk is read by its counts: `-- x` deleted shows as `--- x`, and
    `++ y` inserted as `+++ y`."""
    text = ("diff --git a/n.md b/n.md\n--- a/n.md\n+++ b/n.md\n@@ -1,2 +1,2 @@\n"
            "--- x\n+++ y\n same\n")
    (f,) = D.parse(text)
    assert f.name == "n.md"
    assert [ln[0] for ln in f.hunks[0][1]] == ["-", "+", " "]
    assert f.hunks[0][1][0][3] == "-- x"


def test_runs_pair_line_by_line_and_the_shorter_side_is_blank():
    lines = [(" ", 1, 1, "k"), ("-", 2, None, "a"), ("-", 3, None, "b"),
             ("+", None, 2, "A"), (" ", 4, 3, "k"), ("-", 5, None, "gone"),
             (" ", 6, 4, "k"), ("+", None, 5, "x"), ("+", None, 6, "y")]
    assert D.rows(lines) == [(0, 0), (1, 3), (2, None), (4, 4), (5, None), (6, 6),
                             (None, 7), (None, 8)]


def test_no_newline_mid_run_keeps_the_pair():
    """git's `\\ No newline at end of file` comes between a last line's old
    and new text: the two still sit side by side, the notes after them."""
    text = ("diff --git a/t b/t\n--- a/t\n+++ b/t\n@@ -1 +1 @@\n-old\n"
            "\\ No newline at end of file\n+new\n\\ No newline at end of file\n")
    (f,) = D.parse(text)
    lines = f.hunks[0][1]
    assert [ln[0] for ln in lines] == ["-", "\\", "+", "\\"]
    assert D.rows(lines) == [(0, 2), (None, None, 1), (None, None, 3)]


def test_renames_binaries_and_modes_are_notes_under_the_new_name():
    text = ("diff --git a/old.txt b/new.txt\nsimilarity index 100%\n"
            "rename from old.txt\nrename to new.txt\n"
            "diff --git a/img.png b/img.png\nnew file mode 100644\n"
            "Binary files /dev/null and b/img.png differ\n"
            "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n"
            "+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    files = D.parse(text)
    assert [f.name for f in files] == ["new.txt", "img.png", "run.sh", "gone.py"]
    assert files[0].notes == ["similarity index 100%", "rename from old.txt",
                              "rename to new.txt"]
    assert files[1].notes[-1] == "Binary files /dev/null and b/img.png differ"
    assert files[2].notes == ["old mode 100644", "new mode 100755"]
    assert (files[3].minus, files[3].hunks[0][1]) == (1, [("-", 1, None, "x")])


def test_a_quoted_name_reads_as_the_file_is_named():
    text = ('diff --git "a/caf\\303\\251 x.md" "b/caf\\303\\251 x.md"\n'
            '--- "a/caf\\303\\251 x.md"\n+++ "b/caf\\303\\251 x.md"\n@@ -1 +1 @@\n-a\n+b\n')
    assert D.parse(text)[0].name == "café x.md"


def test_text_before_the_first_file_and_an_empty_diff_read_as_nothing():
    assert D.parse("") == []
    assert D.parse("commit abc\n\nsubject\n") == []


def test_both_modes_escape_and_mark_the_changed_words():
    (f,) = D.parse("diff --git a/h b/h\n--- a/h\n+++ b/h\n@@ -1 +1,2 @@\n"
                   "-x = <b>old</b> + 1\n+x = <b>new</b> + 1\n+<i>alone</i>\n")
    for mode in D.MODES:
        out = D.render([f], mode, "d0")
        assert "<b>" not in out and "&lt;b&gt;" in out
        assert "<i>" not in out and "&lt;i&gt;alone" in out      # a line with no partner
        assert '<span class="sb-dx">old</span>' in out
        assert '<span class="sb-dx">new</span>' in out
    assert "sb-diff-split" in D.render([f], "split", "d0")
    assert "sb-diff-files" not in D.render([f], "unified", "d0")    # one file: no list


def test_a_line_unlike_its_partner_is_not_marked_word_by_word():
    (f,) = D.parse("diff --git a/h b/h\n--- a/h\n+++ b/h\n@@ -1 +1 @@\n"
                   "-import os\n+class Thing(Base):\n")
    assert "sb-dx" not in D.render([f], "split", "d0")


def test_several_files_get_a_list_linking_each_section():
    files = D.parse(TWO_HUNKS + TWO_HUNKS.replace("x.py", "y.py"))
    out = D.render(files, "unified", "d7")
    assert '<a href="#d7-1">y.py</a>' in out and 'id="d7-1"' in out


def _commit(repo, msg):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_a_merge_reads_against_its_first_parent(repo):
    """`git show` gives a clean merge's patch as a combined diff, which is
    empty: the viewer showed a merge's stat and no change at all."""
    (repo / "m.txt").write_text("one\n")
    _commit(repo, "base")
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "side"], check=True)
    (repo / "m.txt").write_text("one\ntwo\n")
    _commit(repo, "side")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    (repo / "other.txt").write_text("o\n")
    _commit(repo, "main moves")
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "-m", "merge side",
                    "side"], check=True)
    # a setting that empties `-m --first-parent`, git's form before 2.31
    subprocess.run(["git", "-C", str(repo), "config", "log.diffMerges", "combined"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    _, _, parents, message, patch = gitref.commit_show(repo, sha)
    assert len(parents) == 2 and message == "merge side"
    (f,) = D.parse(patch)
    assert (f.name, f.plus, f.minus) == ("m.txt", 1, 0)
    assert gitref.commit_files(repo, sha) == [("m.txt", 1, 0)]


def test_a_since_diff_under_autocrlf_shows_only_the_line_that_changed(repo):
    """The left side is a commit's and the right the work tree's: git
    reads both through the file's filters, so CRLF on disk is no change."""
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "true"], check=True)
    (repo / "w.txt").write_bytes(b"a\r\nb\r\nc\r\n")
    _commit(repo, "w")
    (repo / "w.txt").write_bytes(b"a\r\nB\r\nc\r\n")
    oldest = _commit(repo, "edit w")
    blob = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{oldest}^:w.txt"],
                          check=True, capture_output=True, text=True).stdout.strip()
    base, exact = gitref.since_base(repo, "w.txt", oldest, blob)
    assert exact
    (f,) = D.parse(gitref.diff_text(repo, base, "--", ":(literal)w.txt"))
    assert (f.plus, f.minus) == (1, 1)


def test_a_row_that_saw_uncommitted_edits_reads_against_the_nearest_commit(repo):
    (repo / "u.txt").write_text("1\n")
    _commit(repo, "u")
    (repo / "u.txt").write_text("1\n2\n")
    seen = gitref.target_blob(gitref_cfg(repo), "file", "u.txt")
    (repo / "u.txt").write_text("1\n3\n")
    oldest = _commit(repo, "u again")
    base, exact = gitref.since_base(repo, "u.txt", oldest, seen)
    assert not exact
    assert base == subprocess.run(["git", "-C", str(repo), "rev-parse", f"{oldest}^"],
                                  check=True, capture_output=True, text=True).stdout.strip()


def gitref_cfg(repo):
    from symbion.config import Config
    return Config(project_root=repo, catalogs={"file": "git ls-files"})


def test_a_rename_lists_its_new_path(repo):
    (repo / "old.txt").write_text("same\n")
    _commit(repo, "add")
    subprocess.run(["git", "-C", str(repo), "mv", "old.txt", "new.txt"], check=True)
    sha = _commit(repo, "rename")
    assert gitref.commit_files(repo, sha) == [("new.txt", 0, 0)]


def test_a_read_only_serve_keeps_no_diff(repo, monkeypatch):
    """`_git` keeps each answer for good under --read-only: a whole-tree
    diff kept there would hold the demo's memory."""
    monkeypatch.setenv("SYMBION_READ_ONLY", "1")
    (repo / "f").write_text("y\n")
    sha = _commit(repo, "y")
    gitref._ANSWERS.clear()
    assert gitref.diff_text(repo, "HEAD~1", "HEAD")
    assert gitref.commit_show(repo, sha)[4]
    assert not [k for k in gitref._ANSWERS if k[1][0] in ("diff", "show")]
