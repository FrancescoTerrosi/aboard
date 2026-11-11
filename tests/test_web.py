"""Web shell smoke tests — the HTTP backend exposes the same core.

These do not exercise the LLM seams; they verify JSON reads, command
dispatch, and static serving. Async SSE jobs are covered indirectly by the
core/TUI tests.
"""

import tempfile
import unittest

from fastapi.testclient import TestClient

from tuple_space import TupleSpace

from aboard.board import Board
from aboard.core import BoardService
from aboard.web import create_app


class WebShellTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.board = Board(self.space, actor="human")
        self.service = BoardService(board=self.board, human="human")
        self.client = TestClient(create_app(self.service))

    def tearDown(self):
        self.client.close()
        self.space.close()
        self._tmp.cleanup()

    def test_status_and_items_are_served(self):
        self.board.insert("task", instruction="build")
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tasks"], 1)
        r = self.client.get("/api/items/task")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rows"], [["task", "t0", "build"]])

    def test_command_add_returns_table_state(self):
        r = self.client.post("/api/command",
                             json={"line": "/add task hello"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["messages"][0]["message"], "added task t0")
        self.assertEqual(body["table"]["tag"], "task")
        self.assertEqual(body["table"]["rows"], [["task", "t0", "hello"]])

    def test_static_shell_is_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ABoard", r.text)
        r = self.client.get("/static/app.js")
        self.assertEqual(r.status_code, 200)

    def test_files_view_dedupes_latest_record_per_path(self):
        self.space.out("file_created", "a.txt", "1", "success", actor="arch")
        self.space.out("file_created", "a.txt", "2", "success", actor="arch")
        self.space.out("file_created", "b.txt", "3", "success", actor="arch")
        r = self.client.get("/api/files")
        self.assertEqual(r.status_code, 200)
        files = r.json()["files"]
        self.assertEqual([f[1] for f in files], ["a.txt", "b.txt"])
        self.assertEqual(files[0][2], "2")   # latest a.txt, not the first


if __name__ == "__main__":
    unittest.main()
