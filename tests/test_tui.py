"""Smoke tests for the ABoard TUI — headless (no terminal needed).

These verify the app composes and that the table reflects the board, using
textual's ``run_test`` pilot. They exercise navigation wiring (tag switch,
filter, row highlight → detail), not pixel output.
"""

import asyncio
import os
import tempfile
import unittest

from textual.widgets import DataTable, Input, Markdown, Select

from tuple_space import TupleSpace

from aboard import config
from aboard.board import Board
from aboard.chat import ChatAgent
from aboard.session import load_stock, open_space
from aboard.tui import BoardApp
import aboard.core as core_mod
from tests_support import completion, call, ScriptedClient


class TuiSmokeTest(unittest.TestCase):
    def test_composes_and_lists_active_tag(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="summarize")
                board.insert("knowledge", name="n", context="c", text="t")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    # default tag = "task" -> one row
                    self.assertEqual(table.row_count, 1)
                space.close()
        asyncio.run(run())

    def test_switch_tag_reloads_table(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="summarize")
                board.insert("knowledge", name="n")
                board.insert("knowledge", name="m")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._tag = "knowledge"
                    app.query_one("#tag", Select).value = "knowledge"
                    app._setup_columns()
                    app._reload()
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    self.assertEqual(table.row_count, 2)
                space.close()
        asyncio.run(run())

    def test_filter_narrows(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("knowledge", name="api", context="dev", text="JSON")
                board.insert("knowledge", name="ml", context="dev", text="gradient")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._tag = "knowledge"
                    app.query_one("#tag", Select).value = "knowledge"
                    app._setup_columns()
                    app._reload()
                    app._query = "api"
                    app._reload()
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    self.assertEqual(table.row_count, 1)
                space.close()
        asyncio.run(run())

    def test_tab_cycles_panels_and_brackets_switch_tags(self):
        """The keyboard contract: tab/shift+tab move focus between the panels
        (the classic expectation — and the only way out of the command line);
        tag switching lives on ]/[ and 1/2/3."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="summarize")
                board.insert("knowledge", name="n")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # first launch lands on the table: arrows work immediately
                    table = app.query_one("#table", DataTable)
                    self.assertIs(app.focused, table)
                    # tab moves focus, never the tag
                    await pilot.press("tab")
                    await pilot.pause()
                    self.assertEqual(app._tag, "task")
                    self.assertIsNot(app.focused, table)     # focus moved on
                    # shift+tab moves it back
                    await pilot.press("shift+tab")
                    await pilot.pause()
                    self.assertIs(app.focused, table)
                    # the command line is escapable: : → tab lands elsewhere
                    await pilot.press(":")
                    await pilot.pause()
                    self.assertIs(app.focused, app.query_one("#cmd"))
                    await pilot.press("tab")
                    await pilot.pause()
                    self.assertIsNot(app.focused, app.query_one("#cmd"))
                    # ] / [ cycle the tag
                    await pilot.press("right_square_bracket")
                    await pilot.pause()
                    self.assertEqual(app._tag, "knowledge")
                    self.assertEqual(table.row_count, 1)
                    await pilot.press("right_square_bracket"); await pilot.press("right_square_bracket")
                    await pilot.pause()
                    self.assertEqual(app._tag, "task")       # wraps skill → task
                    await pilot.press("left_square_bracket")
                    await pilot.pause()
                    self.assertEqual(app._tag, "skill")       # backward
                    # with the Select focused, documented keys fire — not
                    # swallowed by type-to-search (the old default ate them)
                    sel = app.query_one("#tag", Select)
                    sel.focus()
                    await pilot.pause()
                    await pilot.press("2")
                    await pilot.pause()
                    self.assertEqual(app._tag, "knowledge")
                    await pilot.press("right_square_bracket")
                    await pilot.pause()
                    self.assertEqual(app._tag, "skill")
                    # and with the dropdown open, tag keys close it cleanly
                    sel.expanded = True
                    await pilot.pause()
                    await pilot.press("1")
                    await pilot.pause()
                    self.assertEqual(app._tag, "task")
                    self.assertFalse(sel.expanded)
                space.close()
        asyncio.run(run())

    def test_j_k_move_cursor(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                for i in range(3):
                    board.insert("task", instruction=f"task {i}")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.press("j")
                    self.assertEqual(table.cursor_row, 1)
                    await pilot.press("j")
                    self.assertEqual(table.cursor_row, 2)
                    await pilot.press("k")
                    self.assertEqual(table.cursor_row, 1)
                space.close()
        asyncio.run(run())

    def test_filter_enter_returns_to_table(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="alpha")
                board.insert("task", instruction="beta")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.press("/")   # focus the filter box
                    f = app.query_one("#filter", Input)
                    self.assertIs(app.focused, f)
                    for ch in "alpha":
                        await pilot.press(ch)
                    await pilot.pause()
                    table2 = app.query_one("#table", DataTable)
                    self.assertEqual(table2.row_count, 1)   # narrowed to alpha
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIs(app.focused, table2)    # focus is back on the list
                space.close()
        asyncio.run(run())

    def test_session_note_notified_on_mount(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                note = "resumed 2 item(s) from run 20260101T000000Z-aaaaaaaa"
                app = BoardApp(board=board, note=note)
                seen = []
                app.notify = lambda msg, **kw: seen.append(msg)   # capture toasts
                async with app.run_test() as pilot:
                    await pilot.pause()
                    self.assertIn(note, seen)
                space.close()
        asyncio.run(run())

    def test_chat_turn_runs_and_renders_history(self):
        """The full /chat flow: worker → send → chat tuples in the space →
        detail pane renders the transcript; the board refreshes if the
        agent wrote to it (deterministic via the scripted client)."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                agent = ChatAgent(space, mode="auto", client=ScriptedClient(
                    completion(tool_calls=[call("board_add", {
                        "tag": "task",
                        "values": {"instruction": "chat-created task"}})]),
                    completion(content="added, and here is my reply")))
                app = BoardApp(board=board, chat_agent=agent)
                async with app.run_test() as pilot:
                    app._run_command("/chat add a task for me")
                    for _ in range(20):
                        if space.rdp("chat", "assistant", None) is not None:
                            break
                        await pilot.pause()
                    await pilot.pause()
                    # transcript tuples: human turn + agent reply
                    turns = [(t[1], t[2]) for t in space.read_all("chat", None, None)]
                    self.assertEqual(turns, [
                        ("human", "add a task for me"),
                        ("assistant", "added, and here is my reply")])
                    # the agent's board_add went through the curated template
                    self.assertEqual(board.get("task", "t0"),
                                     ("task", "t0", "chat-created task"))
                    # and the table refreshed to show it
                    table = app.query_one("#table", DataTable)
                    self.assertEqual(table.row_count, 1)
                space.close()
        asyncio.run(run())

    def test_human_mode_propose_then_approve(self):
        """Default mode: the agent stages a proposal; /proposals shows it;
        /approve applies it through the Board — the table gains the row only
        after the human decides."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                agent = ChatAgent(space, client=ScriptedClient(
                    completion(tool_calls=[call("board_propose", {
                        "kind": "add", "tag": "task",
                        "values": {"instruction": "chat-proposed task"}})]),
                    completion(content="proposed — approve when ready")))
                app = BoardApp(board=board, human="human", chat_agent=agent)
                async with app.run_test() as pilot:
                    app._run_command("/chat add a task for me")
                    for _ in range(20):
                        if space.rdp("proposal", None, None, None, None) is not None:
                            break
                        await pilot.pause()
                    await pilot.pause()
                    # staged, but the board (and the table) is untouched
                    table = app.query_one("#table", DataTable)
                    self.assertEqual(table.row_count, 0)
                    self.assertEqual(len(board.all("task")), 0)
                    # /proposals renders the inbox view
                    app._run_command("/proposals")
                    await pilot.pause()
                    detail = app.query_one("#detail_body", Markdown)
                    # /approve applies it: the row appears
                    app._run_command("/approve")
                    await pilot.pause()
                    self.assertEqual(board.get("task", "t0"),
                                     ("task", "t0", "chat-proposed task"))
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(space.rdp("proposal", None, None, None, None),
                                     None)   # decided exactly once
                space.close()
        asyncio.run(run())

    def test_mode_command_switches_live_and_persists(self):
        """/mode auto — a runtime command, not a launch flag: it switches the
        agent live (next turn already runs in the new mode), stores the choice
        in the space, and a resumed board restores it."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                agent = ChatAgent(space, client=ScriptedClient(
                    completion(tool_calls=[call("board_add", {
                        "tag": "task", "values": {"instruction": "direct"}})]),
                    completion(content="added")))
                app = BoardApp(board=board, human="human", chat_agent=agent)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # default mode shown, then switched live
                    app._run_command("/mode")
                    app._run_command("/mode auto")
                    await pilot.pause()
                    self.assertEqual(app.chat_mode, "auto")
                    self.assertEqual(agent.mode, "auto")
                    self.assertEqual(config.get(space, "mode"), "auto")
                    # the next turn writes directly (auto), no proposal staged
                    app._run_command("/chat add something")
                    for _ in range(20):
                        if space.rdp("task", None, None) is not None:
                            break
                        await pilot.pause()
                    self.assertEqual(board.get("task", "t0"),
                                     ("task", "t0", "direct"))
                    self.assertEqual(
                        space.rdp("proposal", None, None, None, None), None)
                space.close()
                # the choice rides session persistence — resume restores it
                space2, _ = open_space(d)
                try:
                    self.assertEqual(config.get(space2, "mode"), "auto")
                finally:
                    space2.close()
        asyncio.run(run())

    def test_mode_command_validates(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                agent = ChatAgent(space, client=ScriptedClient())
                app = BoardApp(board=board, chat_agent=agent)
                toasts = []
                app.notify = lambda msg, **kw: toasts.append(msg)
                async with app.run_test() as pilot:
                    app._run_command("/mode yolo")
                    await pilot.pause()
                    self.assertEqual(app.chat_mode, "human")   # unchanged
                    self.assertTrue(any("must be one of" in t for t in toasts))
                space.close()
        asyncio.run(run())

    def test_stock_command_mounts_shows_and_unmounts(self):
        """/stock <path> mounts at runtime (persisted in the space), /stock
        shows the view, /stock off unmounts."""
        async def run():
            with tempfile.TemporaryDirectory() as d, \
                 tempfile.TemporaryDirectory() as stock_root:
                s = TupleSpace(run_root=stock_root)
                Board(s).insert("knowledge", name="common",
                                context="shared", text="x")
                s.close()

                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # nothing mounted → /import warns with the command hint
                    app._run_command("/import knowledge know0")
                    await pilot.pause()
                    # mount at runtime — the agent picks it up too
                    app._run_command(f"/stock {stock_root}")
                    await pilot.pause()
                    self.assertTrue(app.stock)
                    agent = app._ensure_chat_agent()
                    self.assertEqual(agent.stock, app.stock)
                    self.assertEqual(config.get(space, "stock"), stock_root)
                    # /stock renders the view
                    app._run_command("/stock")
                    await pilot.pause()
                    # /import now works
                    app._run_command("/import knowledge know0")
                    await pilot.pause()
                    self.assertEqual(board.get("knowledge", "know0"),
                                     ("knowledge", "know0", "common", "shared", "x"))
                    # unmount, then re-mount (also the refresh path)
                    app._run_command("/stock off")
                    await pilot.pause()
                    self.assertIsNone(app.stock)
                    self.assertIsNone(config.get(space, "stock"))
                    app._run_command(f"/stock {stock_root}")
                    await pilot.pause()
                    self.assertTrue(app.stock)
                space.close()
                # the mount rides session persistence — resume restores it
                space2, _ = open_space(d)
                try:
                    self.assertEqual(config.get(space2, "stock"), stock_root)
                finally:
                    space2.close()
        asyncio.run(run())

    def test_chat_reply_survives_the_table_reload(self):
        """Regression: /chat used to flash the reply, then the post-turn table
        refresh re-highlighted the same row and overwrote the pane with the
        item detail. A transient view now holds the pane until the cursor
        explicitly moves."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="one")
                board.insert("task", instruction="two")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.pause()    # cursor on row 0 (t0)
                    # show a chat view, then reload exactly as _do_chat does
                    app._detail_update("## chat\n\n**assistant:** THE_ANSWER",
                                       view="chat")
                    app._reload()
                    await pilot.pause()
                    detail = app.query_one("#detail_body", Markdown)
                    self.assertIn("THE_ANSWER",
                                  getattr(detail, "_markdown", "") or "")
                    # moving the cursor to a different row returns item detail
                    await pilot.press("j")
                    await pilot.pause()
                    self.assertNotIn("THE_ANSWER",
                                     getattr(detail, "_markdown", "") or "")
                space.close()
        asyncio.run(run())

    def test_number_keys_jump_tags(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="x")
                board.insert("knowledge", name="n")
                board.insert("skill", name="s")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()   # digits must not type into inputs
                    await pilot.pause()
                    await pilot.press("2")
                    await pilot.pause()
                    self.assertEqual(app._tag, "knowledge")
                    self.assertEqual(table.row_count, 1)
                    await pilot.press("3")
                    self.assertEqual(app._tag, "skill")
                    await pilot.press("1")
                    self.assertEqual(app._tag, "task")
                    # an open dropdown never strands a popup open
                    sel = app.query_one("#tag", Select)
                    sel.expanded = True
                    await pilot.pause()
                    await pilot.press("right_square_bracket")
                    await pilot.pause()
                    self.assertEqual(app._tag, "knowledge")
                    self.assertFalse(sel.expanded)
                space.close()
        asyncio.run(run())

    def test_plan_work_stop_status_flow(self):
        """The full operator loop: create → plan → work → approve."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="build a website")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # /plan with a scripted planner (does the real space ops)
                    async def fake_plan(space, *, tid, llm, actor):
                        space.inp("task", tid, None, actor=actor)
                        space.out("task", f"{tid}.1", "design", actor=actor)
                        space.out("task", f"{tid}.2", "implement", actor=actor)
                        return [f"{tid}.1", f"{tid}.2"]
                    import aboard.tui as tui_mod
                    orig_plan = core_mod.plan
                    core_mod.plan = fake_plan
                    orig_worker_cls = core_mod.TaskWorker
                    class FakeTaskWorker:
                        def __init__(self, space, *, name, llm):
                            self.space = space
                            self.name = name
                            self.llm = llm
                            self.tasks_done = 0
                            self.tokens_used = 0
                            self._stopped = False
                        def stop(self):
                            self._stopped = True
                        async def run(self, on_event=None):
                            t = self.space.inp("task", None, None,
                                               actor=self.name)
                            if t is None:
                                if on_event:
                                    on_event(f"{self.name}: no more tasks (0 done)")
                                return
                            if on_event:
                                on_event(f"{self.name}: task {t[1]} → proposal p0")
                            self.tasks_done += 1
                    core_mod.TaskWorker = FakeTaskWorker
                    try:
                        app._run_command("/plan")   # highlighted row (t0)
                        for _ in range(20):
                            if board.get("task", "t0.1") is not None:
                                break
                            await pilot.pause()
                        self.assertEqual(board.get("task", "t0.2"),
                                         ("task", "t0.2", "implement"))
                        table = app.query_one("#table", DataTable)
                        self.assertEqual(table.row_count, 2)   # parent replaced
                        app._run_command("/work 2")
                        for _ in range(20):
                            if space.read_all("task", None, None) == []:
                                break
                            await pilot.pause()
                        await pilot.pause()
                        self.assertEqual(space.read_all("task", None, None), [])
                        # /status renders the view
                        app._run_command("/status")
                        await pilot.pause()
                        detail = app.query_one("#detail_body", Markdown)
                        self.assertIn("workers:", getattr(detail, "_markdown", "") or "")
                        app._run_command("/stop")
                        await pilot.pause()
                    finally:
                        core_mod.plan = orig_plan
                        core_mod.TaskWorker = orig_worker_cls
                space.close()
        asyncio.run(run())

    def test_agent_commands_define_list_and_start(self):
        """Named agents: /agent add persists a definition, /agent lists it,
        /agent start spawns a role-specialized worker and releases its name
        when it stops."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                import aboard.tui as tui_mod
                orig = core_mod.TaskWorker

                class FakeWorker:
                    def __init__(self, space, *, name, llm,
                                 role=None, skills=None):
                        self.space = space
                        self.name = name
                        self.llm = llm
                        self.role = role
                        self.skills = list(skills or [])
                        self.tokens_used = 0
                        self.tasks_done = 0
                        self._stopped = False
                    def stop(self):
                        self._stopped = True
                    async def run(self, on_event=None):
                        while not self._stopped:
                            await asyncio.sleep(0.01)

                core_mod.TaskWorker = FakeWorker
                try:
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app._run_command("/agent add alice writer writing editing")
                        await pilot.pause()
                        self.assertEqual(space.rdp("agent", "alice", None, None),
                                         ("agent", "alice", "writer",
                                          ["editing", "writing"]))
                        app._run_command("/agent")
                        await pilot.pause()
                        md = getattr(app.query_one("#detail_body", Markdown),
                                     "_markdown", "") or ""
                        self.assertIn("alice", md)
                        self.assertIn("writer", md)
                        app._run_command("/agent start alice")
                        await pilot.pause()
                        self.assertIn("alice", app._task_workers)
                        self.assertEqual(app._task_workers["alice"].role, "writer")
                        self.assertEqual(app._task_workers["alice"].skills,
                                         ["editing", "writing"])
                        app._run_command("/stop alice")
                        await pilot.pause()
                        self.assertNotIn("alice", app._task_workers)
                finally:
                    core_mod.TaskWorker = orig
                space.close()
        asyncio.run(run())

    def test_tag_and_dep_commands(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="one")
                board.insert("task", instruction="two")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._run_command("/tag t0 writing")
                    app._run_command("/dep t1 t0")
                    await pilot.pause()
                    self.assertEqual(space.read_all("task_skill", None, None),
                                     [("task_skill", "t0", "writing")])
                    self.assertEqual(space.read_all("dependency", None, None),
                                     [("dependency", "t1", ["t0"])])
                space.close()
        asyncio.run(run())

    def test_tuples_command_reads_any_tag_and_all(self):
        """Wide-angle inspection: /tuples shows any tag, not just curated."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="one")
                space.out("dependency", "t0", ["t9"], actor="human")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._run_command("/tuples dependency")
                    await pilot.pause()
                    md = getattr(app.query_one("#detail_body", Markdown),
                                 "_markdown", "") or ""
                    self.assertIn("dependency", md)
                    self.assertIn("t9", md)
                    app._run_command("/tuples")
                    await pilot.pause()
                    md = getattr(app.query_one("#detail_body", Markdown),
                                 "_markdown", "") or ""
                    self.assertIn("task", md)
                    self.assertIn("dependency", md)
                space.close()
        asyncio.run(run())

    def test_stop_named_worker(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                import aboard.tui as tui_mod
                orig = core_mod.TaskWorker

                class FakeWorker:
                    def __init__(self, space, *, name, llm,
                                 role=None, skills=None):
                        self.space = space
                        self.name = name
                        self.llm = llm
                        self.role = role
                        self.skills = list(skills or [])
                        self.tokens_used = 0
                        self.tasks_done = 0
                        self._stopped = False
                    def stop(self):
                        self._stopped = True
                    async def run(self, on_event=None):
                        while not self._stopped:
                            await asyncio.sleep(0.01)

                core_mod.TaskWorker = FakeWorker
                try:
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app._run_command("/work 2")
                        await pilot.pause()
                        names = sorted(app._task_workers)
                        self.assertEqual(names, ["w0", "w1"])
                        w0 = app._task_workers["w0"]
                        w1 = app._task_workers["w1"]
                        app._run_command("/stop w0")
                        await pilot.pause()
                        self.assertTrue(w0._stopped)
                        self.assertFalse(w1._stopped)
                        self.assertNotIn("w0", app._task_workers)  # released
                        self.assertIn("w1", app._task_workers)     # still live
                finally:
                    core_mod.TaskWorker = orig
                space.close()
        asyncio.run(run())

    def test_spawn_agents_panel_and_review(self):
        """Tier 3.1/3.2: /spawn tracks a generic agent, /agents renders its
        transcript, /review toasts verdicts, and /stop halts a generic agent."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                import aboard.tui as tui_mod
                orig_generic = core_mod.GenericAgent
                orig_review = core_mod.review

                class FakeGeneric:
                    def __init__(self, space, *, name, role, llm,
                                 skills=(), goal=None):
                        self.space = space
                        self.name = name
                        self.role = role
                        self.skills = list(skills)
                        self.tokens_used = 0
                        self.turns = 0
                        self._stopped = False
                    def stop(self):
                        self._stopped = True
                    async def run(self, on_event=None):
                        self.space.out("agent_turn", self.name, "a turn",
                                       actor=self.name)
                        self.turns = 1
                        if on_event:
                            on_event(f"{self.name}: a turn")
                        while not self._stopped:
                            await asyncio.sleep(0.01)

                async def fake_review(space, *, llm, pid=None, actor=None,
                                      client=None):
                    return ["p0: approve — ok"]

                core_mod.GenericAgent = FakeGeneric
                core_mod.review = fake_review
                toasts = []
                app.notify = lambda msg, **kw: toasts.append(msg)
                try:
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app._run_command("/spawn alice analyst writing")
                        await pilot.pause()
                        self.assertIn("alice", app._llm_agents)
                        self.assertEqual(app._llm_agents["alice"].skills,
                                         ["writing"])
                        # the generic agent wrote a transcript turn
                        self.assertEqual(space.read_all("agent_turn", "alice", None),
                                         [("agent_turn", "alice", "a turn")])
                        app._run_command("/agents")
                        await pilot.pause()
                        detail = app.query_one("#detail_body", Markdown)
                        md = getattr(detail, "_markdown", "") or ""
                        self.assertIn("alice", md)
                        self.assertIn("a turn", md)
                        # reviewer toasts a verdict
                        app._run_command("/review")
                        await pilot.pause()
                        self.assertTrue(any("review: p0: approve" in t
                                            for t in toasts))
                        # /stop halts the generic agent and releases its name
                        alice = app._llm_agents["alice"]
                        app._run_command("/stop alice")
                        await pilot.pause()
                        self.assertTrue(alice._stopped)
                        self.assertNotIn("alice", app._llm_agents)
                finally:
                    core_mod.GenericAgent = orig_generic
                    core_mod.review = orig_review
                space.close()
        asyncio.run(run())

    def test_architect_and_files(self):
        """Tier 3.4: /architect is conversational — start, send a turn, and
        /files renders the project + file records from the space."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                import aboard.tui as tui_mod
                orig = core_mod.ArchitectAgent

                class FakeArchitect:
                    def __init__(self, space, *, name, role, human, llm,
                                 sandbox, skills=None, client=None):
                        self.space = space
                        self.name = name
                        self.role = role
                        self.human = human
                        self.sandbox = sandbox
                        self.skills = list(skills or [])
                        self.tokens_used = 0
                        self.turns = 0
                        if space.rdp("project", name, None) is None:
                            space.out("project", name, sandbox, actor=name)

                    async def send(self, text):
                        self.space.out("architect", self.name, self.human,
                                       text, actor=self.human)
                        self.space.out("architect", self.name, self.name,
                                       "ok", actor=self.name)
                        self.turns += 1
                        self.tokens_used = 7
                        return {"reply": "ok", "tokens_used": 7,
                                "api_calls": 1}

                core_mod.ArchitectAgent = FakeArchitect
                toasts = []
                app.notify = lambda msg, **kw: toasts.append(msg)
                try:
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app._run_command("/architect start arch builder web")
                        await pilot.pause()
                        self.assertIsNotNone(app._architect_agent)
                        a = app._architect_agent
                        self.assertEqual(a.name, "arch")
                        self.assertEqual(a.role, "builder")
                        self.assertEqual(a.skills, ["web"])
                        self.assertEqual(a.sandbox,
                                         os.path.join(d, "projects", "arch"))
                        self.assertTrue(any("architect arch started" in t
                                            for t in toasts))
                        # now talk to it — a human turn + an architect reply
                        app._run_command("/architect scaffold a node app")
                        for _ in range(20):
                            turns = space.read_all("architect", "arch",
                                                   None, None)
                            if len(turns) >= 2:
                                break
                            await pilot.pause()
                        await pilot.pause()
                        self.assertEqual(
                            [(t[2], t[3]) for t in space.read_all(
                                "architect", "arch", None, None)],
                            [("human", "scaffold a node app"),
                             ("arch", "ok")])
                        md = getattr(app.query_one("#detail_body", Markdown),
                                     "_markdown", "") or ""
                        self.assertIn("scaffold a node app", md)
                        self.assertIn("ok", md)
                        # /files shows the project and any file records
                        space.out("file_created", "README.md", "12", "success",
                                  actor="arch")
                        app._run_command("/files")
                        await pilot.pause()
                        md = getattr(app.query_one("#detail_body", Markdown),
                                     "_markdown", "") or ""
                        self.assertIn("arch", md)
                        self.assertIn("README.md", md)
                        self.assertIn("12 bytes", md)
                finally:
                    core_mod.ArchitectAgent = orig
                space.close()
        asyncio.run(run())

    def test_discuss_command_starts_and_shows_transcript(self):
        """Tier 3.2 sandbox: /discuss starts two agents, writes interleaved
        turns, /discuss re-opens the transcript, /stop ends it and frees the
        seats for a fresh discussion in the same session."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                import aboard.tui as tui_mod
                orig = core_mod.Discussion

                class FakeAgent:
                    def __init__(self, name):
                        self.name = name
                        self.role = "discussant"
                        self.skills = []
                        self.tokens_used = 0
                        self.turns = 0
                        self._stopped = False
                    def stop(self):
                        self._stopped = True

                class FakeDiscussion:
                    def __init__(self, space, *, a_name, b_name, llm,
                                 topic="", rounds=3, actor=None,
                                 client_a=None, client_b=None):
                        self.space = space
                        self.a = FakeAgent(a_name)
                        self.b = FakeAgent(b_name)
                        self.topic = topic
                        self.actor = actor
                    def stop(self):
                        self.a.stop()
                        self.b.stop()
                    async def run(self, on_event=None):
                        self.space.out("agent_turn", self.a.name, "hi",
                                       actor=self.a.name)
                        self.space.out("agent_turn", self.b.name, "yo",
                                       actor=self.b.name)
                        self.a.turns = 1
                        self.b.turns = 1
                        if on_event:
                            on_event(f"{self.a.name}: hi")
                        # stay live until stopped, like the real driver
                        while not (self.a._stopped or self.b._stopped):
                            await asyncio.sleep(0.01)

                core_mod.Discussion = FakeDiscussion
                try:
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app._run_command("/discuss alice bob the plan")
                        await pilot.pause()
                        self.assertIn("alice", app._llm_agents)
                        self.assertIn("bob", app._llm_agents)
                        self.assertEqual(len(app._discussions), 1)
                        self.assertEqual(
                            len(space.read_all("agent_turn", None, None)), 2)
                        app._run_command("/discuss")   # re-open the transcript
                        await pilot.pause()
                        detail = app.query_one("#detail_body", Markdown)
                        md = getattr(detail, "_markdown", "") or ""
                        self.assertIn("alice", md)
                        self.assertIn("bob", md)
                        alice = app._llm_agents["alice"]
                        app._run_command("/stop alice")
                        await pilot.pause()
                        self.assertTrue(alice._stopped)
                        # seats are released once the discussion ends
                        self.assertNotIn("alice", app._llm_agents)
                        self.assertNotIn("bob", app._llm_agents)
                        self.assertEqual(len(app._discussions), 0)
                finally:
                    core_mod.Discussion = orig
                space.close()
        asyncio.run(run())

    def test_discuss_no_arg_reads_transcript_from_the_space(self):
        """After a restart there is no in-memory Discussion object, but the
        agent_turn transcript is still in the space and must be readable."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                space.out("agent_turn", "alice", "A1", actor="alice")
                space.out("agent_turn", "bob", "B1", actor="bob")
                board = Board(space, actor="human")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app._run_command("/discuss")
                    await pilot.pause()
                    detail = app.query_one("#detail_body", Markdown)
                    md = getattr(detail, "_markdown", "") or ""
                    self.assertIn("alice", md)
                    self.assertIn("A1", md)
                    self.assertIn("bob", md)
                    self.assertIn("B1", md)
                space.close()
        asyncio.run(run())

    def test_reset_command_twice_confirmed(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="x")
                space.out("config", "mode", "auto", actor="human")
                app = BoardApp(board=board, human="human", chat_mode="auto")
                toasts = []
                app.notify = lambda msg, **kw: toasts.append(msg)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # first /reset arms: counts what would go, fires nothing
                    app._run_command("/reset")
                    await pilot.pause()
                    self.assertTrue(any("again to confirm" in t for t in toasts))
                    self.assertEqual(len(board.all("task")), 1)
                    # any other command disarms
                    app._run_command("/status")
                    await pilot.pause()
                    self.assertFalse(app._reset_armed)
                    # arm and confirm → everything cleared, defaults restored
                    app._run_command("/reset")
                    app._run_command("/reset")
                    await pilot.pause()
                    self.assertEqual(board.all("task"), [])
                    self.assertEqual(space.snapshot(), [])
                    self.assertEqual(app.chat_mode, "human")
                    self.assertTrue(any("board reset" in t for t in toasts))
                space.close()
        asyncio.run(run())

    def test_select_null_event_does_not_crash(self):
        """Regression: Select can emit Changed(Select.NULL) (blank selections
        were allowed); the handler used to assign it into _tag and crash on
        fields(Select.NULL). Now: never blank, and a null event is ignored
        with the display restored."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="x")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    sel = app.query_one("#tag", Select)
                    self.assertFalse(sel._allow_blank)  # cannot be cleared
                    # simulate the null Changed event from the crash report
                    fake_event = type("E", (), {"select": sel,
                                                "value": Select.NULL})()
                    app.on_select_changed(fake_event)   # must not raise
                    await pilot.pause()
                    self.assertEqual(app._tag, "task")
                    self.assertEqual(sel.value, "task")  # display restored
                    # real keys through the widget: open, escape, pick — no crash
                    sel.focus()
                    await pilot.press("enter"); await pilot.pause()
                    await pilot.press("escape"); await pilot.pause()
                    self.assertEqual(app._tag, "task")
                space.close()
        asyncio.run(run())

    def test_help_command_and_question_mark(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    # `?` opens the reference from anywhere
                    await pilot.press("?")
                    await pilot.pause()
                    detail = app.query_one("#detail_body", Markdown)
                    md = getattr(detail, "_markdown", "") or ""
                    self.assertIn("## help", md)
                    for cmd in ("/add", "/del", "/search", "/clear",
                                "/chat", "/mode", "/proposals", "/approve",
                                "/reject", "/plan", "/work", "/stop",
                                "/stock", "/import", "/status", "/reset",
                                "/help"):
                        self.assertIn(cmd, md)
                    # it is a transient view: it survives table reloads
                    app._reload()
                    await pilot.pause()
                    self.assertIn("## help",
                                  getattr(detail, "_markdown", "") or "")
                    # /help is the command form; unknown commands point to it
                    app._run_command("/help")
                    await pilot.pause()
                    self.assertIn("## help",
                                  getattr(detail, "_markdown", "") or "")
                    toasts = []
                    app.notify = lambda msg, **kw: toasts.append(msg)
                    app._run_command("/frobnicate")
                    await pilot.pause()
                    self.assertTrue(any("/help lists all" in t for t in toasts))
                space.close()
        asyncio.run(run())

    def test_detail_pane_is_scrollable_and_in_focus_cycle(self):
        """Regression: Markdown is a plain unscrollable widget in this Textual
        — long items/chat were clipped with no way to read them. The pane is
        now a focusable scroll container in the tab cycle."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("knowledge", name="long",
                             context="c", text="line\n" * 200)
                app = BoardApp(board=board)
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    await pilot.press("2")       # knowledge; highlight know0
                    await pilot.pause()
                    # tab until the detail pane has focus (table → cmd → select
                    # → filter → detail)
                    pane = app.query_one("#detail")
                    for _ in range(5):
                        await pilot.press("tab")
                        await pilot.pause()
                        if app.focused is pane:
                            break
                    self.assertIs(app.focused, pane)
                    self.assertGreater(pane.max_scroll_y, 0)   # content overflows
                    await pilot.press("j")
                    await pilot.press("j")
                    await pilot.pause()
                    self.assertGreater(pane.scroll_y, 0)        # j scrolls the pane
                    await pilot.press("home")
                    await pilot.pause()
                    self.assertEqual(pane.scroll_y, 0)         # home works too
                space.close()
        asyncio.run(run())

    def test_enter_forces_item_detail_over_transient_view(self):
        """A transient view traps the pane only until the cursor moves — and
        enter on the (same!) row is the explicit show-this-tuple action."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="x")
                app = BoardApp(board=board)
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.pause()
                    app._detail_update("## help\n\ntrapped", view="help")
                    await pilot.pause()
                    await pilot.press("enter")     # same row, no cursor move
                    await pilot.pause()
                    detail = app.query_one("#detail_body", Markdown)
                    md = getattr(detail, "_markdown", "") or ""
                    self.assertIn("instruction", md)
                    self.assertNotIn("trapped", md)
                    self.assertEqual(app._current_tid, "t0")
                space.close()
        asyncio.run(run())

    def test_approve_reject_keybindings_and_live_view(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="one")
                from aboard.proposals import submit
                submit(space, "add", "knowledge", {"name": "a", "text": "x"},
                       actor="assistant")
                submit(space, "add", "knowledge", {"name": "b", "text": "y"},
                       actor="assistant")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.pause()
                    # open the proposals view, then decide from the keys
                    app._run_command("/proposals")
                    await pilot.pause()
                    await pilot.press("a")        # approve oldest (p0)
                    await pilot.pause()
                    self.assertEqual(board.get("knowledge", "know0")[2], "a")
                    # the proposals view is refreshed, not stale
                    md = getattr(app.query_one("#detail_body", Markdown),
                                 "_markdown", "") or ""
                    self.assertIn("p1", md)
                    self.assertNotIn("p0:", md)
                    await pilot.press("r")        # reject the rest
                    await pilot.pause()
                    self.assertIsNone(board.get("knowledge", "know1"))
                    from aboard.proposals import pending as pending_props
                    self.assertEqual(pending_props(space), [])
                space.close()
        asyncio.run(run())

    def test_subtitle_shows_proposal_count(self):
        """Pending proposals are always visible — the subtitle shows the count."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                board.insert("task", instruction="one")
                from aboard.proposals import submit
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    await pilot.pause()
                    self.assertIn("0 proposal(s) pending", app.sub_title)
                    submit(space, "add", "knowledge", {"name": "a", "text": "x"},
                           actor="assistant")
                    app._reload()
                    await pilot.pause()
                    self.assertIn("1 proposal(s) pending", app.sub_title)
                space.close()
        asyncio.run(run())

    def test_proposals_view_navigable_and_decides_marked(self):
        """`j` moves the marker; `a`/`r` decide the marked proposal, not always
        the oldest."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space, actor="human")
                from aboard.proposals import submit, pending as pending_props
                submit(space, "add", "knowledge", {"name": "a", "text": "x"},
                       actor="assistant")
                submit(space, "add", "knowledge", {"name": "b", "text": "y"},
                       actor="assistant")
                app = BoardApp(board=board, human="human")
                async with app.run_test() as pilot:
                    table = app.query_one("#table", DataTable)
                    table.focus()
                    await pilot.pause()
                    app._run_command("/proposals")
                    await pilot.pause()
                    # marker starts on p0; move down to p1 and approve it
                    await pilot.press("j")
                    await pilot.pause()
                    md = getattr(app.query_one("#detail_body", Markdown),
                                 "_markdown", "") or ""
                    self.assertIn("▶ p1", md)
                    await pilot.press("a")
                    await pilot.pause()
                    # p1's item landed, p0 is still pending
                    self.assertEqual(board.get("knowledge", "know0")[2], "b")
                    self.assertEqual([t[1] for t in pending_props(space)], ["p0"])
                space.close()
        asyncio.run(run())

    def test_table_shows_content_fields_truncated(self):
        """The task table used to show ids only; every field is a column now,
        truncated for layout, full text in the detail pane."""
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                board.insert("task", instruction="write the README section")
                app = BoardApp(board=board)
                async with app.run_test(size=(120, 24)) as pilot:
                    await pilot.pause()
                    table = app.query_one("#table", DataTable)
                    self.assertEqual([str(c.label) for c in table.columns.values()],
                                     ["id", "instruction"])
                    row = table.get_row_at(0)
                    self.assertEqual(row[1], "write the README section")
                    long = "x" * 500
                    board.insert("knowledge", name=long, context="c", text="t")
                    await pilot.press("2")
                    await pilot.pause()
                    row = table.get_row_at(0)
                    self.assertLess(len(row[1]), 60)          # truncated…
                    self.assertTrue(row[1].endswith("…"))
                    self.assertEqual(board.get("knowledge", "know0")[2], long)
                space.close()
        asyncio.run(run())

    def test_chat_failure_is_a_toast_not_a_crash(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                space = TupleSpace(run_root=d)
                board = Board(space)
                agent = ChatAgent(space, client=ScriptedClient(
                    RuntimeError("LLM endpoint unreachable: refused")))
                app = BoardApp(board=board, chat_agent=agent)
                toasts = []
                app.notify = lambda msg, **kw: toasts.append(msg)
                async with app.run_test() as pilot:
                    app._run_command("/chat hello?")
                    for _ in range(20):
                        if toasts:
                            break
                        await pilot.pause()
                    self.assertTrue(toasts and "chat failed" in toasts[0], toasts)
                space.close()
        asyncio.run(run())

    def test_render_detail_contains_fields(self):
        with tempfile.TemporaryDirectory() as d:
            space = TupleSpace(run_root=d)
            board = Board(space)
            app = BoardApp(board=board)
            html = app._render_detail(("knowledge", "know0", "api", "dev", "JSON body"))
            self.assertIn("api", html)
            self.assertIn("JSON body", html)
            self.assertIn("know0", html)
            space.close()


if __name__ == "__main__":
    unittest.main()
