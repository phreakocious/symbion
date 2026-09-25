"""The lock must span the whole read-modify-write, not just the write.

These fail against an implementation that loads outside the lock.
That is exactly why they are worth writing: the harness issues parallel tool
calls, so several symbion processes in one message is the normal case."""
import multiprocessing as mp
from symbion import store


def _supersede(args):
    path, note_id = args
    store.supersede(path, note_id, body="racer")


def test_concurrent_supersede_does_not_fork_the_chain(tmp_path):
    a = store.add(tmp_path, kind="bug", target={"type": "project", "name": None})
    with mp.Pool(2) as pool:
        pool.map(_supersede, [(str(tmp_path), a.id)] * 2)
    hs = store.heads(store.load(tmp_path))
    assert len(hs) == 1, f"chain forked into {len(hs)} heads"


def _create(args):
    path, name = args
    store.create_arc(path, name, "", "item")


def _rename(args):
    path, old, new = args
    return store.rename_target(path, "item", old, new)[0]


def test_concurrent_rename_target_does_not_double_count(tmp_path):
    """rename_target's own heads()-scan must happen INSIDE the one lock
    spanning its whole sweep, not before it, or two concurrent renames of the
    same name each see all N notes as still unrenamed and each reports N --
    double-counting work that only happened once. (Per-note forking is
    already prevented by supersede()'s own fast-forward regardless; this
    tests the AGGREGATE's atomicity as one transaction, not the per-note
    write.)"""
    n = 30
    for _ in range(n):
        store.add(tmp_path, kind="note", target={"type": "item", "name": "old"})
    with mp.Pool(2) as pool:
        counts = pool.map(_rename, [(str(tmp_path), "old", "new")] * 2)
    assert sum(counts) == n, f"expected exactly {n} total renames, got {counts}"
    heads = store.heads(store.load(tmp_path))
    names = {h.target.name for h in heads if h.target.type == "item"}
    assert names == {"new"}, f"chain forked or left stragglers: {names}"


def test_concurrent_arc_creation_loses_nothing(tmp_path):
    """The registry is the one file with no supersede history to reconstruct
    from, so a lost rewrite is unrecoverable. Exercises create_arc:
    a registry loaded outside the lock lets two racing creates
    both read the same registry snapshot and one rewrite clobbers the other."""
    with mp.Pool(2) as pool:
        pool.map(_create, [(str(tmp_path), "alpha"), (str(tmp_path), "beta")])
    names = {a.name for a in store.load_arcs(tmp_path)}
    assert names == {"alpha", "beta"}, f"registry rewrite dropped one: {names}"
