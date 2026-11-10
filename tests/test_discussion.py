"""Discussion (Tier 3.2 sandbox) — deterministic tests through the client seam.

Verifies two named agents alternate turns through the tuple space, each turn's
context carries the other's recent words, and the loop ends on stop / empty
turn / budget. No network — both agents' clients are scripted.
"""

import asyncio
import json
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.chat import LLMConfig
from aboard.discussion import Discussion
from tests_support import ScriptedClient, completion


class DiscussionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.llm = LLMConfig(model="test-model")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def _records(self):
        self.space.flush_log()
        with open(self.space.ops_jsonl_path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_driver_reads_are_actor_attributed(self):
        ca = ScriptedClient(completion("A1"))
        cb = ScriptedClient(completion("B1"))
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       topic="topic", rounds=1, actor="human",
                       client_a=ca, client_b=cb)
        asyncio.run(d.run())
        reads = [r for r in self._records()
                 if r["kind"] == "read" and r["op"] == "READ_ALL"]
        # every context-building read is attributed to the discussion's actor
        self.assertTrue(reads)
        self.assertTrue(all(r["actor"] == "human" for r in reads))

    def test_alternates_turns_and_records_interleaved_transcript(self):
        ca = ScriptedClient(completion("A thinks X"), completion("A agrees"))
        cb = ScriptedClient(completion("B thinks Y"), completion("B concurs"))
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       topic="the board", rounds=2, client_a=ca, client_b=cb)
        events = []
        asyncio.run(d.run(on_event=events.append))
        self.assertEqual(d.a.turns, 2)
        self.assertEqual(d.b.turns, 2)
        turns = self.space.read_all("agent_turn", None, None)
        self.assertEqual([t[1] for t in turns], ["alice", "bob", "alice", "bob"])
        self.assertEqual(turns[0][2], "A thinks X")
        self.assertTrue(any("discussion ended" in e for e in events))

    def test_each_turn_context_carries_the_others_words(self):
        ca = ScriptedClient(completion("A1"), completion("A2"))
        cb = ScriptedClient(completion("B1"), completion("B2"))
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       topic="topic", rounds=2, client_a=ca, client_b=cb)
        asyncio.run(d.run())
        # bob's first turn context = alice's opening turn ("A1")
        user = cb.calls[0]["messages"][-1]["content"]
        self.assertIn("alice said", user)
        self.assertIn("A1", user)
        self.assertIn("Respond as bob", user)
        # alice's second turn context = bob's reply ("B1")
        user = ca.calls[1]["messages"][-1]["content"]
        self.assertIn("bob said", user)
        self.assertIn("B1", user)

    def test_stopping_one_agent_ends_the_discussion(self):
        ca = ScriptedClient(completion("A1"), completion("A2"), completion("A3"))
        cb = ScriptedClient(completion("B1"), completion("B2"), completion("B3"))
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       rounds=5, client_a=ca, client_b=cb)
        d.b.stop()  # bob stops before his first turn
        events = []
        asyncio.run(d.run(on_event=events.append))
        self.assertEqual(d.a.turns, 1)   # alice opened, then the loop stopped
        self.assertEqual(d.b.turns, 0)
        self.assertTrue(any("discussion ended" in e for e in events))

    def test_empty_turn_ends_the_discussion(self):
        ca = ScriptedClient(completion("A1"))
        cb = ScriptedClient(completion(""))  # bob has nothing to say
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       rounds=5, client_a=ca, client_b=cb)
        asyncio.run(d.run())
        self.assertEqual(d.a.turns, 1)
        self.assertEqual(d.b.turns, 0)

    def test_opening_context_has_topic_and_opener_hint(self):
        ca = ScriptedClient(completion("A1"))
        cb = ScriptedClient(completion("B1"))
        d = Discussion(self.space, a_name="alice", b_name="bob", llm=self.llm,
                       topic="plan the release", rounds=1, client_a=ca, client_b=cb)
        asyncio.run(d.run())
        # alice (first speaker) gets the topic + "you open"
        user = ca.calls[0]["messages"][-1]["content"]
        self.assertIn("plan the release", user)
        self.assertIn("You open the discussion as alice", user)


if __name__ == "__main__":
    unittest.main()
