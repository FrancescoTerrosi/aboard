"""Architect (Tier 3.4) — a conversational, sandboxed file-capable agent.

Verifies the refactored architect: it declares its project idempotently, wires
the substrate's file tools under a sandbox with a read-only tuple-space
toolset, refuses remote boards, records its conversation as
``("architect", name, speaker, text)`` turns, and writes files through the
tool loop with ``file_created`` records in the space.
"""

import asyncio
import os
import tempfile
import unittest

from tuple_space import TupleSpace
from tuple_space.llm_client import FunctionCall, ToolCall

from aboard.architect import ArchitectAgent
from aboard.chat import LLMConfig
from aboard.models import ARCHITECT, FILE_CREATED, PROJECT
from tests_support import ScriptedClient, call, completion


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

    def test_system_prompt_is_chat_facing(self):
        a = ArchitectAgent(self.space, name="arch", role="builder",
                           llm=self.llm, sandbox=self.sandbox)
        prompt = a._agent.system_prompt
        self.assertIn("chatting with a human", prompt)
        self.assertIn("create_file", prompt)
        # the substrate's autonomous coordination instructions are replaced
        self.assertNotIn("claim tasks", prompt)

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
