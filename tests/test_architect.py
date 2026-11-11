"""Architect (Tier 3.4) — a conversational, sandboxed file-capable agent.

Verifies the refactored architect: it declares its project idempotently, wires
the substrate's file tools under a sandbox with a read-only tuple-space
toolset, refuses remote boards, records its conversation as
``("architect", name, speaker, text)`` turns, and writes files through the
tool loop with ``file_created`` records in the space.
"""

import asyncio
import json
import os
import tempfile
import unittest

from tuple_space import TupleSpace
from tuple_space.llm_client import FunctionCall, ToolCall

from aboard.architect import ArchitectAgent
from aboard.board import Board
from aboard.chat import LLMConfig
from aboard.core import BoardService
from aboard.models import ARCHITECT, FILE_CREATED, PROJECT
from tests_support import ScriptedClient, call, completion


class ArchitectDeliveryTest(unittest.TestCase):
    """Service level: a file-writing turn is detectable from the board.

    Delivery is enforced in code (never via the model): every file the
    architect writes lands as a ``knowledge`` tuple, attributed to the
    architect, so the knowledge tab/filter/search find it.
    """

    class _Observer:
        def __init__(self):
            self.messages = []

        def on_message(self, message, *, level="info"):
            self.messages.append((message, level))

        def on_view(self, state):
            pass

        def on_table(self, state):
            pass

        def on_state(self):
            pass

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.board = Board(self.space, actor="human")
        self.sandbox = os.path.join(self._tmp.name, "sandbox")
        self.llm = LLMConfig(model="test-model")
        agent = ArchitectAgent(self.space, name="arch", role="builder",
                               llm=self.llm, sandbox=self.sandbox,
                               client=ScriptedClient(
                                   completion(tool_calls=[
                                       call("create_file", {
                                           "filepath": "README.md",
                                           "content": "# hi"})]),
                                   completion(content="done")))
        self.service = BoardService(board=self.board, human="human",
                                    architect_agent=agent)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_file_turn_is_enforced_as_knowledge(self):
        obs = self._Observer()
        asyncio.run(self.service.do_architect("write a README", obs))
        # detection: the file is on the board, in the curated knowledge tag
        know = self.space.read_all("knowledge", None, None, None, None)
        self.assertEqual(len(know), 1)
        tag, kid, name, context, text = know[0]
        self.assertEqual((tag, name, context),
                         ("knowledge", "README.md", "architect arch"))
        self.assertIn("4 bytes", text)
        self.assertIn(self.sandbox, text)
        # the toast names the delivered file and its knowledge id
        self.assertTrue(any("file(s) → know0" in m
                            for m, _ in obs.messages), obs.messages)
        # attribution: the WAL records the architect as the writer
        self.space.flush_log()
        with open(self.space.ops_jsonl_path) as f:
            records = [json.loads(l) for l in f if l.strip()]
        muts = [r for r in records if r["kind"] == "mutation"
                and r["args"][0] == "knowledge"]
        self.assertEqual({r["actor"] for r in muts}, {"arch"})

    def test_no_file_turn_writes_no_knowledge(self):
        agent = ArchitectAgent(self.space, name="arch", role="builder",
                               llm=self.llm, sandbox=self.sandbox,
                               client=ScriptedClient(
                                   completion(content="just chatting")))
        self.service._architect_agent = agent
        obs = self._Observer()
        asyncio.run(self.service.do_architect("what do you think?", obs))
        self.assertEqual(self.space.read_all("knowledge", None, None,
                                              None, None), [])


class ArchitectAgentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.llm = LLMConfig(model="test-model")
        self.sandbox = os.path.join(self._tmp.name, "sandbox")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_declares_project_and_wires_read_file_tools(self):
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox,
                           skills=["web"])
        self.assertEqual(self.space.read_all(PROJECT, None, None),
                         [("project", "arch", self.sandbox)])
        self.assertEqual(a._agent.output_dir, self.sandbox)
        self.assertTrue(a._agent.enable_file_tool)
        self.assertEqual(a._agent.skills, ["web"])
        names = {t["function"]["name"] for t in a._agent._tools}
        # reads + file writes only — the architect navigates, it does not
        # mutate the board through the model's tools
        self.assertIn("rdp", names)
        self.assertIn("read_all", names)
        self.assertIn("create_file", names)
        self.assertIn("scaffold_files", names)
        for forbidden in ("out", "inp", "in_", "signal_done"):
            self.assertNotIn(forbidden, names)

    def test_project_declaration_is_idempotent(self):
        ArchitectAgent(self.space, name="arch", role="builder",
                       llm=self.llm, sandbox=self.sandbox)
        # re-opening the same named architect must not duplicate the project
        ArchitectAgent(self.space, name="arch", role="builder",
                       llm=self.llm, sandbox=self.sandbox)
        self.assertEqual(self.space.read_all(PROJECT, None, None),
                         [("project", "arch", self.sandbox)])

    def test_remote_board_is_refused(self):
        class Remote:
            _remote = True

        with self.assertRaises(ValueError):
            ArchitectAgent(Remote(), name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox)

    def test_send_records_human_and_architect_turns(self):
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox,
                           client=ScriptedClient(
                               completion(content="scaffolded")))
        result = asyncio.run(a.send("make a README"))
        self.assertEqual(result["reply"], "scaffolded")
        self.assertEqual(a.turns, 1)
        self.assertEqual(
            self.space.read_all(ARCHITECT, "arch", None, None),
            [("architect", "arch", "human", "make a README"),
             ("architect", "arch", "arch", "scaffolded")])

    def test_send_creates_files_under_sandbox(self):
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox,
                           client=ScriptedClient(
                               completion(tool_calls=[
                                   call("create_file", {
                                       "filepath": "README.md",
                                       "content": "# hi"})]),
                               completion(content="done")))
        result = asyncio.run(a.send("write a README"))
        self.assertEqual(result["reply"], "done")
        # the turn reports its own delivery — the detection seam
        self.assertEqual(result["files"],
                         [{"path": "README.md", "bytes": 4}])
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "README.md")))
        with open(os.path.join(self.sandbox, "README.md")) as f:
            self.assertEqual(f.read(), "# hi")
        self.assertEqual(self.space.read_all(FILE_CREATED, None, None, None),
                         [("file_created", "README.md", "4", "success")])

    def test_tool_calls_only_final_message_still_records_a_reply(self):
        """If the model ends on tool calls, the transcript must not leave the
        human turn unanswered — the UI would show nothing back."""
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox,
                           client=ScriptedClient(
                               completion(tool_calls=[
                                   call("create_file", {
                                       "filepath": "a.txt",
                                       "content": "x"})]),
                               completion(content=None)))
        result = asyncio.run(a.send("make a.txt"))
        self.assertIn("ok: a.txt", result["reply"])
        turns = self.space.read_all(ARCHITECT, "arch", None, None)
        self.assertEqual([t[2] for t in turns], ["human", "arch"])
        self.assertIn("ok: a.txt", turns[-1][3])

    def test_file_creation_through_the_tool_loop(self):
        """Tool calls execute in the file-capable loop; recorded; reply lands.

        Run under a *rehydrated over-budget* state — the exact session shape
        after the budget policy changed to chat parity: an architect whose
        recorded usage already exceeds the cap must keep scaffolding files.
        """
        space = self.space
        llm = LLMConfig(model="test-model", token_budget=500)
        first = ArchitectAgent(space, name="arch", role="builder", llm=llm,
                               sandbox=self.sandbox,
                               client=ScriptedClient(
                                   completion(content="hi", total=600)))
        asyncio.run(first.send("hello"))
        self.assertEqual(first.tokens_used, 600)
        space.flush_log()
        space.close()

        # a resumed board restores the architect with the spent budget —
        # via the session's recovery path (replay of the prior run's WAL)
        space = TupleSpace.recover(first.space.run_dir,
                                   run_root=self._tmp.name)
        a = ArchitectAgent(space, name="arch", role="builder", llm=llm,
                           sandbox=self.sandbox,
                           client=ScriptedClient(
                               completion(tool_calls=[
                                   call("create_file", {
                                       "filepath": "plan.md",
                                       "content": "# plan"})]),
                               completion(content="written")))
        self.assertEqual(a.tokens_used, 600)  # rehydrated, still tracked
        result = asyncio.run(a.send("scaffold the plan"))
        self.assertTrue(result["over_budget"])
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "plan.md")))
        with open(os.path.join(self.sandbox, "plan.md")) as f:
            self.assertEqual(f.read(), "# plan")
        self.assertEqual([t[3] for t in space.read_all(
            ARCHITECT, "arch", None, None)][-1], "written")

    def test_system_prompt_is_chat_facing(self):
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox)
        prompt = a._agent.system_prompt
        self.assertIn("chatting with a human", prompt)
        self.assertIn("create_file", prompt)
        # delivery rule: a file request must produce the file, never a
        # description of it (the model skipping tool calls was the failure)
        self.assertIn("the tool call IS the work", prompt)
        # the substrate's autonomous coordination instructions are replaced
        self.assertNotIn("claim tasks", prompt)

    def test_over_budget_reports_but_keeps_answering(self):
        # chat parity: cost is tracked and reported, never enforced — the
        # turn that crosses the budget completes, the next turn is answered
        a = ArchitectAgent(
            self.space, name="arch", role="builder",
            llm=LLMConfig(model="test-model", token_budget=500),
            sandbox=self.sandbox,
            client=ScriptedClient(completion(content="hi", total=50),
                                  completion(content="done", total=600)))
        first = asyncio.run(a.send("first"))
        self.assertEqual(first["reply"], "hi")
        self.assertFalse(first["over_budget"])
        second = asyncio.run(a.send("second"))
        self.assertEqual(second["reply"], "done")
        self.assertTrue(second["over_budget"])
        # the conversation stays complete — nothing refused mid-work
        self.assertEqual(self.space.read_all(ARCHITECT, "arch", None, None),
                         [("architect", "arch", "human", "first"),
                          ("architect", "arch", "arch", "hi"),
                          ("architect", "arch", "human", "second"),
                          ("architect", "arch", "arch", "done")])

    def test_budget_state_is_restored_on_reopen(self):
        llm = LLMConfig(model="test-model", token_budget=500)
        a = ArchitectAgent(self.space, name="arch", role="builder", llm=llm,
                           sandbox=self.sandbox,
                           client=ScriptedClient(completion(content="hi",
                                                            total=600)))
        asyncio.run(a.send("first"))
        # a fresh instance for the same name resumes the spent budget —
        # usage is rehydrated for reporting, but never bricks the agent
        b = ArchitectAgent(self.space, name="arch", role="builder", llm=llm,
                           sandbox=self.sandbox,
                           client=ScriptedClient(completion(content="no")))
        self.assertEqual(b.tokens_used, 600)
        self.assertFalse(b._agent._budget_exceeded)
        result = asyncio.run(b.send("second"))
        self.assertEqual(result["reply"], "no")
        self.assertTrue(result["over_budget"])
        # the transcript stays complete across the reopen
        self.assertEqual(self.space.read_all(ARCHITECT, "arch", None, None),
                         [("architect", "arch", "human", "first"),
                          ("architect", "arch", "arch", "hi"),
                          ("architect", "arch", "human", "second"),
                          ("architect", "arch", "arch", "no")])

    def test_read_all_is_field_bounded_but_rdp_is_full(self):
        self.space.out("knowledge", "know0", "n", "ctx", "x" * 1000,
                      actor="human")
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox)
        broad = a._execute_read("read_all",
                                {"template": ["knowledge", None, None,
                                              None, None]})
        self.assertIn("…", broad)
        self.assertLess(len(broad), 1200)
        targeted = a._execute_read("rdp",
                                   {"template": ["knowledge", "know0", None,
                                                 None, None]})
        self.assertIn("x" * 1000, targeted)

    def test_malformed_tool_call_does_not_abort_the_turn(self):
        client = ScriptedClient(
            completion(tool_calls=[
                ToolCall(id="c0", function=FunctionCall(
                    name="rdp", arguments="{not json"))]),
            completion(content="recovered"))
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox, client=client)
        result = asyncio.run(a.send("look around"))
        self.assertEqual(result["reply"], "recovered")
        # the malformed call became a tool result, not a crash
        tool_msgs = [m for m in client.calls[1]["messages"]
                     if m["role"] == "tool"]
        self.assertTrue(tool_msgs)
        self.assertIn("malformed arguments", tool_msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
