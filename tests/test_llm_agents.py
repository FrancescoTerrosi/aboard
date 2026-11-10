"""Generic agents (Tier 3.1) — deterministic tests through the client seam.

Verifies the substrate ``LLMAgent`` wrapper: it steps, records each turn as
an ``agent_turn`` tuple, tracks budget, honors stop, and registers skills as
capabilities on the board. No network — the substrate agent's transport seam
(``agent.client``) is swapped for a scripted client.
"""

import asyncio
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.chat import LLMConfig
from aboard.llm_agents import GenericAgent
from tests_support import ScriptedClient, completion


class GenericAgentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.llm = LLMConfig(model="test-model")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def _agent(self, **kw):
        return GenericAgent(self.space, name="alice", role="analyst",
                            llm=self.llm, **kw)

    def test_runs_and_records_transcript(self):
        a = self._agent(client=ScriptedClient(
            completion(content="I observed the board")))
        events = []
        asyncio.run(a.run(on_event=events.append, max_steps=1))
        self.assertEqual(a.turns, 1)
        self.assertEqual(a.tokens_used, 10)
        self.assertEqual(self.space.read_all("agent_turn", "alice", None),
                         [("agent_turn", "alice", "I observed the board")])
        self.assertTrue(any("step limit reached" in e for e in events))

    def test_multiple_turns(self):
        a = self._agent(client=ScriptedClient(completion("one"),
                                              completion("two")))
        asyncio.run(a.run(max_steps=2))
        self.assertEqual(a.turns, 2)
        self.assertEqual([t[2] for t in self.space.read_all("agent_turn",
                                                            "alice", None)],
                         ["one", "two"])

    def test_stop_before_run(self):
        a = self._agent(client=ScriptedClient(completion("x")))
        a.stop()
        events = []
        asyncio.run(a.run(on_event=events.append))
        self.assertEqual(a.turns, 0)
        self.assertTrue(any("stopped" in e for e in events))

    def test_skills_registered_as_capabilities(self):
        a = self._agent(skills=["writing", "editing"],
                        client=ScriptedClient(completion("x")))
        asyncio.run(a.run(max_steps=1))
        # the substrate's out_capability → ("capability", name, [skills])
        self.assertEqual(self.space.read_all("capability", "alice", None),
                         [("capability", "alice", ["editing", "writing"])])

    def test_failure_does_not_record_a_turn(self):
        a = self._agent(client=ScriptedClient(RuntimeError("endpoint down")))
        events = []
        asyncio.run(a.run(on_event=events.append, max_steps=1))
        self.assertEqual(a.turns, 0)
        self.assertEqual(self.space.read_all("agent_turn", "alice", None), [])
        self.assertTrue(any("failed" in e for e in events))


if __name__ == "__main__":
    unittest.main()
