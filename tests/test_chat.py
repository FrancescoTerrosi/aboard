"""Chat agent — deterministic tests through the client seam.

The LLM is the only nondeterminism, so these tests inject a scripted client
(returning substrate ``Completion`` dataclasses) and verify the *properties*
the design claims:

- writes go through the curated Board (templates enforced, ids autogen);
- every mutation is attributed in the WAL (human turns vs agent ops);
- history is rebuilt from the space (a resumed board resumes the chat);
- cost is tracked and reported, never enforced (no hard chat budget cap).
"""

import asyncio
import json
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.chat import (MODES, ChatAgent, LLMConfig,
                         _auto_tool_schema, _human_tool_schema,
                         _read_tool_schema, _stock_tool_schema, _tool_schema)
from aboard.board import Board
from aboard.session import load_stock
from tests_support import ScriptedClient, call, completion


class ChatAgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def _records(self):
        self.space.flush_log()
        with open(self.space.ops_jsonl_path) as f:
            return [json.loads(l) for l in f if l.strip()]

    # ── the protocol ───────────────────────────────────────────────────

    def test_send_writes_both_turns_and_returns_reply(self):
        agent = ChatAgent(self.space, client=ScriptedClient(
            completion(content="hello!")))
        result = asyncio.run(agent.send("hi"))
        self.assertEqual(result["reply"], "hello!")
        self.assertEqual(result["tokens_used"], 10)
        self.assertEqual(agent.history(),
                         [("chat", "human", "hi"), ("chat", "assistant", "hello!")])

    def test_real_message_history_from_the_space(self):
        # pre-existing turns (e.g. restored by session persistence)
        ChatAgent(self.space).write_turn("human", "earlier question")
        ChatAgent(self.space).write_turn("assistant", "earlier answer")
        client = ScriptedClient(completion(content="ok"))
        agent = ChatAgent(self.space, client=client)
        asyncio.run(agent.send("and now?"))
        msgs = client.calls[0]["messages"]
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "assistant", "user"])
        # the current board view rides on the last (just-written) user turn
        self.assertIn("[current board]", msgs[-1]["content"])

    # existing tests below use auto mode (direct writes); human mode is the
    # default and has its own tests — see HumanModeTest and StockTest.

    def test_board_add_uses_templates_and_autogen_ids(self):
        client = ScriptedClient(
            completion(tool_calls=[call("board_add", {
                "tag": "knowledge",
                "values": {"name": "API note", "context": "dev", "text": "…"},
            })]),
            completion(content="noted."))
        agent = ChatAgent(self.space, mode="auto", client=client)
        result = asyncio.run(agent.send("remember the API note"))
        self.assertEqual(agent.board.get("knowledge", "know0"),
                         ("knowledge", "know0", "API note", "dev", "…"))
        self.assertEqual(result["reply"], "noted.")
        # the tool result told the model the generated id
        tool_msg = client.calls[1]["messages"][-1]
        self.assertEqual(tool_msg["content"], "ok: knowledge know0")

    def test_invalid_add_is_an_error_string_not_a_crash(self):
        client = ScriptedClient(
            completion(tool_calls=[call("board_add", {
                "tag": "bogus", "values": {"x": "y"}})]),
            completion(content="cannot."))
        agent = ChatAgent(self.space, mode="auto", client=client)
        asyncio.run(agent.send("add junk"))
        tool_msg = client.calls[1]["messages"][-1]
        self.assertTrue(tool_msg["content"].startswith("error:"), tool_msg)

    def test_search_and_get_and_claim(self):
        Board(self.space).insert("knowledge", name="a", context="dev", text="x")
        Board(self.space).insert("knowledge", name="b", context="ops", text="y")
        client = ScriptedClient(
            completion(tool_calls=[call("board_search", {
                "tag": "knowledge", "values": {"context": "dev"}})]),
            completion(tool_calls=[call("board_get", {
                "tag": "knowledge", "id": "know1"})]),
            completion(tool_calls=[call("board_claim", {
                "tag": "knowledge", "id": "know0"})]),
            completion(content="done"))
        agent = ChatAgent(self.space, mode="auto", client=client)
        asyncio.run(agent.send("search then get then claim"))
        tool_results = [m["content"] for m in client.calls[3]["messages"]
                        if m["role"] == "tool"]
        self.assertIn("'a'", tool_results[0])            # search locked context
        self.assertNotIn("'b'", tool_results[0])
        self.assertIn("know1", tool_results[1])           # get by id
        self.assertEqual(tool_results[2], "'knowledge', 'know0', 'a', 'dev', 'x'")
        self.assertIsNone(agent.board.get("knowledge", "know0"))  # claimed

    def test_tool_schema_is_curated_only(self):
        # reads are identical in both modes; no raw Linda primitives anywhere
        reads = {t["function"]["name"] for t in _read_tool_schema()}
        self.assertEqual(reads, {"board_list", "board_get", "board_search",
                                 "agent_turns"})
        for mode in MODES:
            for t in _tool_schema(mode, None):
                params = t["function"]["parameters"]["properties"]
                self.assertNotIn("fields", params)  # no raw out() escape hatch

    def test_agent_turns_tool_reads_the_transcript(self):
        self.space.out("agent_turn", "alice", "hello", actor="alice")
        self.space.out("agent_turn", "bob", "hi", actor="bob")
        agent = ChatAgent(self.space)
        result = asyncio.run(agent._execute("agent_turns", {}))
        self.assertIn("alice: hello", result)
        self.assertIn("bob: hi", result)
        # bounded: limit clamps to the most recent turns
        self.assertEqual(asyncio.run(agent._execute("agent_turns", {"limit": 1})),
                         "bob: hi")

    def test_mode_schemas_split_write_access(self):
        human = {t["function"]["name"] for t in _tool_schema("human", None)}
        auto = {t["function"]["name"] for t in _tool_schema("auto", None)}
        self.assertEqual(human - auto, {"board_propose", "board_proposals"})
        self.assertEqual(auto - human, {"board_add", "board_claim"})

    def test_stock_tools_only_when_mounted(self):
        self.assertNotIn("stock_list", {t["function"]["name"]
                                        for t in _tool_schema("human", None)})
        with_stock = {t["function"]["name"]
                     for t in _tool_schema("human", [("task", "t0", "x")])}
        self.assertIn("stock_list", with_stock)
        self.assertIn("stock_search", with_stock)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            ChatAgent(self.space, mode="yolo")

    # ── attribution (E5 property, carried into ABoard) ────────────────

    def test_every_mutation_is_attributed(self):
        client = ScriptedClient(
            completion(tool_calls=[call("board_add", {
                "tag": "task", "values": {"instruction": "do it"}})]),
            completion(content="added."))
        agent = ChatAgent(self.space, mode="auto", client=client)
        asyncio.run(agent.send("add a task"))
        muts = [r for r in self._records() if r["kind"] == "mutation"]
        by_args = {(r["op"], r["actor"]) for r in muts}
        self.assertIn(("OUT", "human"), by_args)       # the human's chat turn
        self.assertIn(("OUT", "assistant"), by_args)    # agent's board_add + reply
        self.assertNotIn(("OUT", None), by_args)       # zero unattributed writes

    # ── cost tracking (no hard cap) ──────────────────────────────────

    def test_chat_has_no_hard_budget_cap(self):
        client = ScriptedClient(completion(total=600),
                                completion(total=600))
        agent = ChatAgent(self.space, client=client,
                          llm=LLMConfig(token_budget=500))
        first = asyncio.run(agent.send("first"))
        second = asyncio.run(agent.send("second"))
        self.assertEqual(first["tokens_used"], 600)
        self.assertEqual(second["tokens_used"], 1200)  # keeps accumulating
        self.assertEqual(len(client.calls), 2)          # both sends went through

    # ── unreachable endpoint (the TUI shows this as an error toast) ────

    def test_endpoint_error_propagates_as_runtime_error(self):
        client = ScriptedClient(RuntimeError("LLM endpoint unreachable: x"))
        agent = ChatAgent(self.space, client=client)
        with self.assertRaises(RuntimeError):
            asyncio.run(agent.send("hi"))
        # the human's turn is still logged — the transcript stays honest
        self.assertEqual(agent.history(), [("chat", "human", "hi")])


class HumanModeTest(unittest.TestCase):
    """Default mode: the agent proposes; the board changes only on approval."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_propose_stages_without_mutating_the_board(self):
        client = ScriptedClient(
            completion(tool_calls=[call("board_propose", {
                "kind": "add", "tag": "knowledge",
                "values": {"name": "API note", "context": "dev", "text": "…"}})]),
            completion(content="I proposed it — approve when ready."))
        agent = ChatAgent(self.space, client=client)   # default mode = human
        asyncio.run(agent.send("remember the API note"))
        # staged, attributed to the agent; the board itself is untouched
        self.assertEqual(self.space.read_all("proposal", None, None, None, None),
                         [("proposal", "p0", "add", "knowledge",
                           {"name": "API note", "context": "dev", "text": "…"},
                           None)])
        self.assertEqual(self.space.read_all("knowledge", None, None, None, None), [])
        tool_msg = client.calls[1]["messages"][-1]
        self.assertIn("pending the human's approval", tool_msg["content"])

    def test_defensive_reject_of_direct_writes(self):
        client = ScriptedClient(
            completion(tool_calls=[call("board_add", {
                "tag": "task", "values": {"instruction": "x"}})]),
            completion(content="sorry"))
        agent = ChatAgent(self.space, client=client)
        asyncio.run(agent.send("add a task directly"))
        tool_msg = client.calls[1]["messages"][-1]
        self.assertIn("disabled in human-driven mode", tool_msg["content"])
        self.assertEqual(self.space.read_all("task", None, None), [])

    def test_approval_chain_in_the_wal(self):
        import json as _json
        from aboard.board import Board
        from aboard.proposals import apply
        client = ScriptedClient(
            completion(tool_calls=[call("board_propose", {
                "kind": "add", "tag": "task",
                "values": {"instruction": "do it"}})]),
            completion(content="proposed"))
        agent = ChatAgent(self.space, client=client)
        asyncio.run(agent.send("add a task"))
        apply(self.space, Board(self.space, actor="human"), actor="human")
        self.space.flush_log()
        recs = [_json.loads(l) for l in open(self.space.ops_jsonl_path) if l.strip()]
        chain = [(r["op"], r["actor"], r["kind"]) for r in recs
                 if r["op"] in ("OUT", "INP") and r["args"][0] in ("proposal", "task")]
        self.assertEqual(chain, [
            ("OUT", "assistant", "mutation"),   # agent proposed
            ("INP", "human", "mutation"),       # human decided (atomic take)
            ("OUT", "human", "mutation"),       # human applied via the Board
        ])


class StockTest(unittest.TestCase):
    """Stock mounts: read-only citation for the agent, import for the human."""

    def setUp(self):
        self._stock_tmp = tempfile.TemporaryDirectory()
        self._space_tmp = tempfile.TemporaryDirectory()
        self.stock_root = self._stock_tmp.name
        self.space = TupleSpace(run_root=self._space_tmp.name)

    def tearDown(self):
        self.space.close()
        self._space_tmp.cleanup()
        self._stock_tmp.cleanup()

    def _stock_board(self, n=1):
        """Curate a stock board (another ABoard board's runs root), closed."""
        space = TupleSpace(run_root=self.stock_root)
        board = Board(space)
        for i in range(n):
            board.insert("knowledge", name=f"common {i}", context="shared",
                         text=f"note {i}")
        space.close()
        return load_stock(self.stock_root)

    def test_stock_tools_cite_without_writing(self):
        stock = self._stock_board(n=2)
        client = ScriptedClient(
            completion(tool_calls=[call("stock_search", {
                "tag": "knowledge", "values": {"name": "common 1"}})]),
            completion(content="the stock says: note 1"))
        agent = ChatAgent(self.space, mode="human", stock=stock, client=client)
        asyncio.run(agent.send("what does the stock say about common 1?"))
        tool_msg = client.calls[1]["messages"][-1]
        self.assertIn("'common 1'", tool_msg["content"])
        self.assertIn("note 1", tool_msg["content"])
        # the stock was never written to the project board
        self.assertEqual(self.space.read_all("knowledge", None, None, None, None), [])

    def test_import_copies_with_new_id_and_attribution(self):
        import json as _json
        stock = self._stock_board(n=1)
        board = Board(self.space, actor="human")
        new_id = board.import_from(stock, "knowledge", "know0")
        self.assertEqual(new_id, "know0")   # first item in this board
        self.assertEqual(board.get("knowledge", "know0"),
                         ("knowledge", "know0", "common 0", "shared", "note 0"))
        self.space.flush_log()
        recs = [_json.loads(l) for l in open(self.space.ops_jsonl_path) if l.strip()]
        outs = [r for r in recs if r["op"] == "OUT" and r["args"][0] == "knowledge"]
        self.assertEqual([r["actor"] for r in outs], ["human"])  # the importer

    def test_import_missing_id_raises(self):
        stock = self._stock_board(n=1)
        board = Board(self.space, actor="human")
        with self.assertRaises(ValueError):
            board.import_from(stock, "knowledge", "know9")


class LoadStockTest(unittest.TestCase):
    def test_load_stock_curates_tags_and_is_lenient(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as stock_root:
            space = TupleSpace(run_root=stock_root)
            board = Board(space)
            board.insert("knowledge", name="common", context="shared", text="x")
            space.out("chat", "human", "stock board chatter")  # non-curated
            space.close()
            stock = load_stock(stock_root)
            self.assertEqual(stock,
                             [("knowledge", "know0", "common", "shared", "x")])
            # unreadable stock: warn + empty, never block the project board
            run = sorted(os.listdir(stock_root))[0]
            ops = os.path.join(stock_root, run, "ops.jsonl")
            lines = open(ops).read().splitlines()
            open(ops, "w").write("\n".join([lines[0], "{not json", *lines[1:]]))
            with self.assertWarns(UserWarning):
                self.assertEqual(load_stock(stock_root), [])
            # missing root: just empty
            self.assertEqual(load_stock("/nonexistent-stock-root-xyz"), [])


if __name__ == "__main__":
    unittest.main()