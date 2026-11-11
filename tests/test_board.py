import json
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard.board import Board, board_view


class BoardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.board = Board(self.space)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    # ── insert: enforced template + autogen id ─────────────────────────

    def test_insert_task(self):
        tid = self.board.insert("task", instruction="summarize the README")
        self.assertEqual(tid, "t0")
        self.assertEqual(self.space.rdp("task", tid),
                         ("task", "t0", "summarize the README"))

    def test_insert_knowledge_and_skill(self):
        self.assertEqual(self.board.insert("knowledge", name="a", context="c", text="t"),
                         "know0")
        self.assertEqual(self.board.insert("skill", name="s", context="c", text="t"),
                         "skill0")

    def test_ids_are_independent_per_tag(self):
        self.board.insert("task", instruction="x")
        self.board.insert("knowledge", name="n")
        self.assertEqual(self.board.insert("task", instruction="y"), "t1")
        self.assertEqual(self.board.insert("skill", name="s"), "skill0")

    def test_insert_ids_unique_under_concurrency(self):
        # atomic id reservation: concurrent writers never hand out the same id
        import threading
        results = []
        results_lock = threading.Lock()

        def worker():
            tid = self.board.insert("task", instruction="x")
            with results_lock:
                results.append(tid)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 50)           # all unique
        self.assertEqual(len(self.board.all("task")), 50)  # all landed

    def test_insert_optional_fields_default_empty(self):
        self.board.insert("knowledge", name="n")  # context/text omitted
        self.assertEqual(self.space.rdp("knowledge", "know0"),
                         ("knowledge", "know0", "n", "", ""))

    def test_insert_validates(self):
        with self.assertRaises(ValueError):
            self.board.insert("result", x="y")          # not curated
        with self.assertRaises(ValueError):
            self.board.insert("knowledge", name="   ")  # empty key field
        with self.assertRaises(ValueError):
            self.board.insert("task", instruction="")   # empty key field
        with self.assertRaises(TypeError):
            self.board.insert("knowledge", name=123)    # non-str
        with self.assertRaises(TypeError):
            self.board.insert("knowledge", name="n", bogus="x")  # unknown field

    # ── search: exact + wildcard ────────────────────────────────────────

    def test_search_wildcard(self):
        self.board.insert("knowledge", name="alpha", context="dev", text="one")
        self.board.insert("knowledge", name="beta", context="dev", text="two")
        self.assertEqual([t[2] for t in self.board.search("knowledge", name="alpha")],
                         ["alpha"])
        self.assertEqual(self.board.search("knowledge", name="gamma"), [])

    def test_search_task(self):
        self.board.insert("task", instruction="do X")
        self.board.insert("task", instruction="do Y")
        got = self.board.search("task", instruction="do Y")
        self.assertEqual([t[2] for t in got], ["do Y"])

    def test_all_returns_everything(self):
        self.board.insert("task", instruction="a")
        self.board.insert("task", instruction="b")
        self.assertEqual(len(self.board.all("task")), 2)

    # ── filter: client-side substring (navigation only) ────────────────

    def test_filter_substring_case_insensitive(self):
        self.board.insert("knowledge", name="api", context="dev", text="the API returns JSON")
        self.board.insert("knowledge", name="ml", context="dev", text="gradient descent")
        got = self.board.filter("knowledge", "api")
        self.assertEqual([t[2] for t in got], ["api"])

    def test_filter_by_field(self):
        self.board.insert("knowledge", name="one", context="backend", text="x")
        self.board.insert("knowledge", name="two", context="frontend", text="x")
        got = self.board.filter("knowledge", "front", field="context")
        self.assertEqual([t[2] for t in got], ["two"])

    def test_filter_task_by_id(self):
        self.board.insert("task", instruction="x")
        self.board.insert("task", instruction="y")
        got = self.board.filter("task", "t0", field="id")
        self.assertEqual([t[1] for t in got], ["t0"])

    # ── remove ─────────────────────────────────────────────────────────

    def test_remove_takes_tuple(self):
        tid = self.board.insert("knowledge", name="a")
        self.board.remove("knowledge", tid)
        self.assertIsNone(self.space.rdp("knowledge", tid))


class BoardViewTest(unittest.TestCase):
    """``board_view`` is a bounded, newest-first briefing for agents."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.board = Board(self.space)

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_newest_first_with_true_total_and_overflow_count(self):
        for i in range(10):
            self.board.insert("task", instruction=f"task {i}")
        view = board_view(self.space, per_tag=3)
        self.assertIn("task: 10 items", view)          # count, not slice length
        self.assertIn("'t9'", view)                     # LIFO: newest first
        self.assertIn("'t8'", view)
        self.assertIn("'t7'", view)
        self.assertNotIn("'t0'", view)
        self.assertIn("(+7 more)", view)                # the unseen remainder

    def test_insertion_order_is_opt_in(self):
        self.board.insert("task", instruction="first")
        self.board.insert("task", instruction="second")
        view = board_view(self.space, newest_first=False)
        self.assertLess(view.index("'t0'"), view.index("'t1'"))

    def test_read_is_bounded_not_just_rendered(self):
        class Probe:
            def __init__(self):
                self.read_calls = []
                self.count_calls = []

            def count(self, *template, actor=None):
                self.count_calls.append(template)
                return 3

            def read_all(self, *template, actor=None, limit=None, reverse=False):
                self.read_calls.append((template, limit, reverse))
                return [("task", f"t{i}", "x") for i in range(limit or 0)]

        probe = Probe()
        board_view(probe, per_tag=8)
        self.assertEqual(len(probe.count_calls), 3)      # one count per tag
        self.assertTrue(all(limit == 8 for _, limit, _ in probe.read_calls))
        self.assertTrue(all(reverse is True for _, _, reverse in probe.read_calls))


class BoardAttributionTest(unittest.TestCase):
    """``actor=`` threads to the WAL: every mutation and read is attributed."""

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

    def test_default_board_is_unattributed_compat(self):
        Board(self.space).insert("task", instruction="x")
        recs = self._records()
        muts = [r for r in recs if r["kind"] == "mutation"]
        self.assertTrue(muts)
        self.assertTrue(all(r["actor"] is None for r in muts))

    def test_actor_attributed_on_mutations_and_reads(self):
        board = Board(self.space, actor="human")
        board.insert("task", instruction="x")
        board.get("task", "t0")
        board.search("task", instruction="x")
        board.remove("task", "t0")
        # every mutation and read is attributed (session_start predates the
        # board's ops and is rightly unattributed)
        recs = [r for r in self._records()
                if r["kind"] in ("mutation", "read")]
        self.assertTrue(recs)
        for r in recs:
            self.assertEqual(r["actor"], "human", r)

    def test_two_boards_share_a_space_with_separate_attribution(self):
        human = Board(self.space, actor="human")
        agent = Board(self.space, actor="assistant")
        human.insert("task", instruction="x")
        agent.insert("task", instruction="y")
        by_actor = {(r["args"][2], r["actor"])
                    for r in self._records()
                    if r["op"] == "OUT" and r["args"] and r["args"][0] == "task"}
        self.assertEqual(by_actor,
                         {("x", "human"), ("y", "assistant")})


if __name__ == "__main__":
    unittest.main()
