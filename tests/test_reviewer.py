"""Reviewer (Tier 3.2) — deterministic tests through the client seam.

Verifies the reviewer reads pending proposals and writes a verdict tuple per
proposal, parsing approve/reject/abstain and safely abstaining on garbage.
No network — the client seam is injected.
"""

import asyncio
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.chat import LLMConfig
from aboard.proposals import submit
from aboard.reviewer import review, _parse_verdict
from tests_support import ScriptedClient, completion


class ReviewerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.llm = LLMConfig(model="test-model")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def _proposal(self):
        return submit(self.space, "add", "knowledge",
                      {"name": "n", "context": "c", "text": "t"}, actor="agent")

    def test_writes_an_approve_verdict(self):
        self._proposal()
        results = asyncio.run(review(
            self.space, llm=self.llm, actor="human",
            client=ScriptedClient(completion("APPROVE looks good"))))
        self.assertEqual(results, ["p0: approve — looks good"])
        self.assertEqual(self.space.read_all("review", "p0", None, None),
                         [("review", "p0", "approve", "looks good")])

    def test_writes_a_reject_verdict(self):
        self._proposal()
        asyncio.run(review(
            self.space, llm=self.llm, actor="human",
            client=ScriptedClient(completion("REJECT not needed"))))
        self.assertEqual(self.space.read_all("review", "p0", None, None),
                         [("review", "p0", "reject", "not needed")])

    def test_unparseable_reply_is_a_safe_abstain(self):
        self._proposal()
        results = asyncio.run(review(
            self.space, llm=self.llm,
            client=ScriptedClient(completion("huh?"))))
        self.assertEqual(results, ["p0: abstain — huh?"])
        self.assertEqual(self.space.read_all("review", "p0", None, None),
                         [("review", "p0", "abstain", "huh?")])

    def test_no_pending_proposals(self):
        results = asyncio.run(review(
            self.space, llm=self.llm, client=ScriptedClient()))
        self.assertEqual(results, ["no pending proposals"])
        self.assertEqual(self.space.read_all("review", None, None, None), [])

    def test_pid_filters_to_one(self):
        p0 = self._proposal()
        p1 = submit(self.space, "add", "task", {"instruction": "x"},
                    actor="agent")
        results = asyncio.run(review(
            self.space, llm=self.llm, pid=p1,
            client=ScriptedClient(completion("APPROVE ok"))))
        self.assertEqual(results, ["p1: approve — ok"])
        self.assertEqual(self.space.read_all("review", p1, None, None),
                         [("review", "p1", "approve", "ok")])
        # p0 got no verdict (filtered out)
        self.assertEqual(self.space.read_all("review", p0, None, None), [])

    def test_review_context_includes_board_and_sibling_proposals(self):
        self.space.out("knowledge", "know0", "n", "c", "existing", actor="human")
        self._proposal()
        submit(self.space, "add", "task", {"instruction": "x"}, actor="agent")
        client = ScriptedClient(completion("APPROVE ok"),
                                completion("REJECT no"))
        asyncio.run(review(self.space, llm=self.llm, actor="human",
                           client=client))
        # first review brief: board view + the OTHER proposal + the target
        brief = client.calls[0]["messages"][1]["content"]
        self.assertIn("[current board]", brief)
        self.assertIn("existing", brief)          # board grounding
        self.assertIn("Other pending proposals", brief)
        self.assertIn("add task", brief)

    def test_parse_verdict_direct(self):
        self.assertEqual(_parse_verdict("APPROVE yes"), ("approve", "yes"))
        self.assertEqual(_parse_verdict("reject: bad"), ("reject", "bad"))
        self.assertEqual(_parse_verdict(""), ("abstain", "(no rationale)"))


if __name__ == "__main__":
    unittest.main()
