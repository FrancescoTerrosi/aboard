"""Session persistence — resume the latest board across launches.

Correctness is read from the tuple-space state (the substrate's contract:
recovery = replay), never from prose. Every test uses its own temporary
runs root; the env-var tests save/restore the process environment.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tuple_space import TupleSpace, load_run, replay_state

from aboard.board import Board
from aboard.config import store as config_store
from aboard.session import (archive_prior_runs, open_space, prior_runs,
                            reset, runs_root)

_ENV_VARS = ("TUPLE_SPACE_ROOT", "TUPLE_SPACE_LOG")


class RunsRootTest(unittest.TestCase):
    """The discovery root must mirror the substrate's creation-side
    resolution exactly, or resume would look where creation never wrote."""

    def _with_clean_env(fn):
        def wrapper(self):
            saved = {v: os.environ.pop(v, None) for v in _ENV_VARS}
            try:
                fn(self)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return wrapper

    def test_explicit_arg_wins_and_is_used_as_is(self):
        self.assertEqual(runs_root("/x"), Path("/x"))

    @_with_clean_env
    def test_default(self):
        self.assertEqual(runs_root(None), Path(".tuplespace") / "runs")

    @_with_clean_env
    def test_env_var_names_the_root(self):
        os.environ["TUPLE_SPACE_ROOT"] = "/env/root"
        self.assertEqual(runs_root(None), Path("/env/root") / "runs")


class PriorRunsTest(unittest.TestCase):
    def test_missing_root_is_empty(self):
        self.assertEqual(prior_runs(Path("/nonexistent-xyz")), [])

    def test_newest_first_and_only_real_runs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("20260101T000001Z-aaaaaaaa",     # older run
                         "20260102T000001Z-bbbbbbbb",     # newer run
                         "not-a-run",                    # no timestamp scheme
                         "2026010XT000001Z-cccccccc"):   # bad timestamp
                (root / name).mkdir()
            (root / "ops.jsonl").write_text("{}")        # stray file
            got = prior_runs(root)
            self.assertEqual([p.name for p in got],
                             ["20260102T000001Z-bbbbbbbb",
                              "20260101T000001Z-aaaaaaaa"])


class OpenSpaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_board(self, n=2):
        """One closed session that inserted ``n`` tasks."""
        space = TupleSpace(run_root=self.root)
        board = Board(space)
        for i in range(n):
            board.insert("task", instruction=f"task {i}")
        space.close()

    # ── resume ─────────────────────────────────────────────────────────

    def test_no_prior_runs_starts_fresh_and_silent(self):
        space, note = open_space(self.root)
        try:
            self.assertEqual(len(space), 0)
            self.assertIsNone(note)
        finally:
            space.close()

    def test_resumes_latest_board(self):
        self._make_board(n=2)
        space, note = open_space(self.root)
        try:
            self.assertEqual(len(Board(space).all("task")), 2)
            self.assertIn("resumed 2 item(s)", note)
            # ids continue across the restart — no collision with t0/t1
            self.assertEqual(Board(space).insert("task", instruction="third"), "t2")
        finally:
            space.close()

    def test_board_survives_multiple_sessions(self):
        self._make_board(n=1)                       # session 1: one task
        s2, _ = open_space(self.root)               # session 2: resume + add
        Board(s2).insert("task", instruction="second")
        s2.close()
        s3, note = open_space(self.root)            # session 3: everything there
        try:
            self.assertEqual([t[2] for t in Board(s3).all("task")],
                             ["task 0", "second"])
            self.assertIn("resumed 2 item(s)", note)
        finally:
            s3.close()

    def test_empty_runs_do_not_shadow_the_board(self):
        self._make_board(n=2)                       # the real board (older)
        TupleSpace(run_root=self.root).close()       # launch-and-quit (newer)
        space, _ = open_space(self.root)
        try:
            self.assertEqual(len(Board(space).all("task")), 2)
        finally:
            space.close()

    def test_env_root_resume_agrees_with_creation(self):
        with tempfile.TemporaryDirectory() as env_root:
            saved = {v: os.environ.pop(v, None) for v in _ENV_VARS}
            os.environ["TUPLE_SPACE_ROOT"] = env_root
            try:
                s = TupleSpace()                     # creation honors the env root
                Board(s).insert("task", instruction="x")
                s.close()
                space, note = open_space(None)       # discovery must agree
                try:
                    self.assertEqual(len(Board(space).all("task")), 1)
                    self.assertIn("resumed 1 item(s)", note)
                finally:
                    space.close()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    # ── fresh flag ──────────────────────────────────────────────────────

    def test_fresh_starts_empty_and_leaves_runs_untouched(self):
        self._make_board(n=2)
        space, note = open_space(self.root, fresh=True)
        try:
            self.assertEqual(len(space), 0)
            self.assertIn("1 prior run(s)", note)
            # the fresh space is its own new run; the old run is untouched
            runs = prior_runs(runs_root(self.root))
            self.assertEqual(len(runs), 2)
            self.assertEqual(len([t for t in replay_state(load_run(runs[-1]))
                                 if t[0] == "task"]), 2)
        finally:
            space.close()

    # ── strict on corruption ─────────────────────────────────────────────

    def test_same_second_runs_ordered_by_mtime_not_uuid(self):
        # name prefixes tie on the second; the random uuid suffix must not
        # decide which run is "latest" (mtime does). Regression: a resume that
        # immediately opens its own run collides with its source's timestamp.
        root = Path(self.root)
        ts = "20260101T000000Z"
        for uuid_hex, item, mtime in (("ffffffff", "old", 1000.0),
                                      ("00000000", "new", 2000.0)):
            d = root / f"{ts}-{uuid_hex}"
            d.mkdir()
            (d / "ops.jsonl").write_text(json.dumps({
                "v": 1, "seq": 1, "op": "OUT", "kind": "mutation",
                "args": ["task", "t0", item], "result": "t1",
            }) + "\n")
            os.utime(d, (mtime, mtime))
        space, note = open_space(self.root)
        try:
            self.assertEqual(space.rdp("task", "t0"),
                             ("task", "t0", "new"))  # the mtime-newer run
        finally:
            space.close()

    def _ops_path(self):
        return prior_runs(runs_root(self.root))[0] / "ops.jsonl"

    def test_corrupt_wal_warns_and_starts_fresh(self):
        self._make_board(n=2)
        ops = self._ops_path()
        lines = ops.read_text().splitlines()
        ops.write_text("\n".join([lines[0], "{not json", *lines[1:]]))
        with self.assertWarns(UserWarning):
            space, note = open_space(self.root)
        try:
            self.assertEqual(len(space), 0)
            self.assertIn("starting fresh", note)
        finally:
            space.close()

    def test_seq_gap_refuses_prefix_replay(self):
        self._make_board(n=3)
        ops = self._ops_path()
        lines = ops.read_text().splitlines()
        del lines[len(lines) // 2]                   # mid-file gap, valid JSON
        ops.write_text("\n".join(lines))
        with self.assertWarns(UserWarning):
            space, note = open_space(self.root)
        try:
            self.assertEqual(len(space), 0)          # never a partial board
            self.assertIn("starting fresh", note)
        finally:
            space.close()


class ResetTest(unittest.TestCase):
    """Durable reset: clear content, keep settings, archive prior runs.

    The key property: a reset survives restarts. Clearing alone would be
    undone by resume (empty runs are skipped in favor of older non-empty
    ones) — archiving is what makes it stick.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_and_close(self):
        s = TupleSpace(run_root=self.root)
        b = Board(s, actor="human")
        b.insert("task", instruction="one")
        b.insert("knowledge", name="n", context="c", text="t")
        s.out("chat", "human", "hi", actor="human")
        s.out("proposal", "p0", "add", "task", {"instruction": "x"},
              actor="assistant")
        config_store(s, "mode", "auto", actor="human")
        s.close()

    def test_reset_clears_content_and_settings(self):
        self._seed_and_close()
        s, _ = open_space(self.root)
        try:
            result = reset(s, actor="human")
            self.assertEqual(result["cleared"]["task"], 1)
            self.assertEqual(result["cleared"]["knowledge"], 1)
            self.assertEqual(result["cleared"]["chat"], 1)
            self.assertEqual(result["cleared"]["proposal"], 1)
            self.assertEqual(result["cleared"]["config"], 1)
            b = Board(s)
            for tag in ("task", "knowledge", "skill"):
                self.assertEqual(b.all(tag), [])
            self.assertEqual(s.read_all("chat"), [])
            self.assertEqual(s.read_all("proposal"), [])
            # factory reset: settings are gone too (defaults on next launch)
            self.assertEqual(s.snapshot(), [])
        finally:
            s.close()

    def test_reset_is_durable_across_resume(self):
        # THE property: without archiving, the next launch would resurrect
        # the old board (the emptied run is skipped, older runs win)
        self._seed_and_close()
        s, _ = open_space(self.root)
        reset(s, actor="human")
        s.close()
        s, note = open_space(self.root)
        try:
            self.assertEqual(len(s), 0)   # nothing resurrected, no settings left
            self.assertIsNone(note)      # it is a *fresh* launch, not a resume
        finally:
            s.close()

    def test_archive_is_reversible(self):
        self._seed_and_close()
        s, _ = open_space(self.root)          # run B (resumed copy of A)
        reset(s, actor="human")
        s.close()
        root = Path(self.root)
        archived = list((root / "archive").iterdir())
        self.assertEqual(len(archived), 1)     # A is under archive/, intact
        # restoring: move it back → resume finds it again
        shutil.move(str(archived[0]), str(root / archived[0].name))
        s, note = open_space(self.root)
        try:
            self.assertEqual([t[2] for t in Board(s).all("task")], ["one"])
            self.assertIn("resumed", note)
        finally:
            s.close()

    def test_archive_prior_runs_leaves_current_alone(self):
        self._seed_and_close()
        s, _ = open_space(self.root)
        try:
            before = [p.name for p in prior_runs(Path(self.root))]
            n = archive_prior_runs(s)
            after = [p.name for p in prior_runs(Path(self.root))]
            self.assertEqual(n, 1)
            self.assertEqual(after, [before[0]])   # the current (newest) stays
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()