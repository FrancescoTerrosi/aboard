"""Proposals — the staging channel (human-driven mode), deterministically.

E5's semantics, carried into ABoard and read from the tuple-space state:
the agent proposes, the human decides (exactly one decision per proposal),
and applying goes through the curated Board attributed to the approver.
"""

import json
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.board import Board
from aboard.proposals import PROPOSAL, apply, describe, pending, reject, submit


class SubmitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_pid_autogen_monotonic(self):
        self.assertEqual(submit(self.space, "add", "task",
                                {"instruction": "x"}, actor="assistant"), "p0")
        self.assertEqual(submit(self.space, "add", "task",
                                {"instruction": "y"}, actor="assistant"), "p1")
        # decided proposals free nothing: p2 even after p0 is applied
        apply(self.space, Board(self.space, actor="human"), "p0", actor="human")
        self.assertEqual(submit(self.space, "add", "task",
                                {"instruction": "z"}, actor="assistant"), "p2")

    def test_submit_validates_before_writing(self):
        for bad in [
            ("frobnicate", "task", {"instruction": "x"}),   # unknown kind
            ("add", "result", {"x": "y"}),                 # non-curated tag
            ("add", "knowledge", {"name": "n", "bogus": "x"}),  # unknown field
            ("add", "knowledge", {"name": "   "}),         # empty key field
            ("add", "knowledge", {"name": 42}),            # non-str value
            ("add", "knowledge", "not a dict"),            # values not a dict
            ("claim", "task", {}),                         # claim without id
        ]:
            with self.assertRaises(ValueError):
                submit(self.space, *bad, actor="assistant")
        # nothing was written: the staging channel stayed clean
        self.assertEqual(pending(self.space), [])

    def test_pending_survives_restart_via_persistence(self):
        # proposals are mutations like any other → replayed on resume
        submit(self.space, "add", "task", {"instruction": "x"}, actor="assistant")
        self.space.flush_log()
        recs = [json.loads(l) for l in open(self.space.ops_jsonl_path) if l.strip()]
        # the proposal write itself is attributed to the assistant
        # (id reservation may add its own counter OUTs, also attributed)
        prop_outs = [r for r in recs
                     if r["op"] == "OUT" and r["args"] and r["args"][0] == "proposal"]
        self.assertEqual([r["actor"] for r in prop_outs], ["assistant"])

    def test_pending_read_is_actor_attributed(self):
        submit(self.space, "add", "task", {"instruction": "x"}, actor="assistant")
        pending(self.space, actor="reader")
        self.space.flush_log()
        recs = [json.loads(l) for l in open(self.space.ops_jsonl_path) if l.strip()]
        reads = [r for r in recs
                 if r["op"] == "READ_ALL" and r["args"][:1] == ["proposal"]
                 and len(r["args"]) == 5]
        self.assertTrue(reads)
        self.assertTrue(all(r["actor"] == "reader" for r in reads))


class DecideTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.board = Board(self.space, actor="human")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_apply_add_goes_through_the_board(self):
        submit(self.space, "add", "knowledge",
               {"name": "API note", "context": "dev", "text": "…"},
               actor="assistant")
        msg = apply(self.space, self.board, actor="human")
        self.assertEqual(msg, "p0: added knowledge know0")
        self.assertEqual(self.board.get("knowledge", "know0"),
                         ("knowledge", "know0", "API note", "dev", "…"))
        self.assertEqual(pending(self.space), [])       # decided, exactly once

    def test_apply_writes_derived_provenance(self):
        submit(self.space, "add", "knowledge",
               {"name": "result t0", "context": "task t0: summarize",
                "text": "summary"},
               actor="assistant", source={"tag": "task", "id": "t0"})
        apply(self.space, self.board, actor="human")
        self.assertEqual(self.space.read_all("derived", "know0", "task", "t0"),
                         [("derived", "know0", "task", "t0")])

    def test_apply_claim_removes(self):
        self.board.insert("task", instruction="do it")
        submit(self.space, "claim", "task", {"id": "t0"}, actor="assistant")
        self.assertEqual(apply(self.space, self.board, actor="human"),
                         "p0: claimed task t0")
        self.assertIsNone(self.board.get("task", "t0"))

    def test_apply_claim_of_missing_id(self):
        submit(self.space, "claim", "task", {"id": "t99"}, actor="assistant")
        self.assertEqual(apply(self.space, self.board, actor="human"),
                         "p0: no task t99 on the board")

    def test_apply_specific_and_oldest(self):
        submit(self.space, "add", "task", {"instruction": "a"}, actor="assistant")
        submit(self.space, "add", "task", {"instruction": "b"}, actor="assistant")
        self.assertEqual(apply(self.space, self.board, "p1", actor="human"),
                         "p1: added task t0")
        self.assertEqual(apply(self.space, self.board, actor="human"),
                         "p0: added task t1")   # no pid → oldest pending

    def test_exactly_one_decision_even_for_two_approvers(self):
        submit(self.space, "add", "task", {"instruction": "a"}, actor="assistant")
        first = apply(self.space, self.board, actor="human-1")
        second = apply(self.space, self.board, actor="human-2")
        self.assertEqual(first, "p0: added task t0")
        self.assertEqual(second, "no pending proposals")  # inp decided it once

    def test_reject_applies_nothing(self):
        submit(self.space, "add", "task", {"instruction": "a"}, actor="assistant")
        self.assertEqual(reject(self.space, actor="human"), "p0: rejected")
        self.assertEqual(len(self.board.all("task")), 0)
        self.assertEqual(reject(self.space, actor="human"),
                         "no pending proposals")

    def test_apply_unknown_pid(self):
        self.assertEqual(apply(self.space, self.board, "p9", actor="human"),
                         "no proposal p9")

    def test_describe(self):
        t = ("proposal", "p0", "add", "knowledge",
             {"name": "API note", "context": "dev", "text": "…"})
        self.assertEqual(describe(t),
                         "p0: add knowledge (name='API note', context='dev', "
                         "text='…')")
        self.assertEqual(describe(("proposal", "p1", "claim", "task", {"id": "t0"})),
                         "p1: claim task t0")
        # 6-field proposal carries provenance, rendered in the one-liner
        t6 = ("proposal", "p2", "add", "knowledge",
              {"name": "r", "context": "task t0", "text": "…"},
              {"tag": "task", "id": "t0"})
        self.assertEqual(describe(t6),
                         "p2: add knowledge (name='r', context='task t0', "
                         "text='…') ← task t0")
        # a claim with provenance shows it too (consistency, not just adds)
        self.assertEqual(describe(("proposal", "p3", "claim", "task",
                                   {"id": "t1"}, {"tag": "task", "id": "t0"})),
                         "p3: claim task t1 ← task t0")


if __name__ == "__main__":
    unittest.main()