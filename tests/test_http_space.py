"""HttpSpace — the cross-process board adapter (Tier 3.3 first slice).

Deterministic tests: a real uvicorn server (in-process thread) backs the
adapter; no network outside the loopback and no LLM. Verifies the adapter
presents the same surface ``Board``/``workers`` use, converts JSON↔tuples
correctly, recomposes ``are_dependencies_met`` over the wire, and hands out
unique autogen ids across concurrent clients.

Requires the ``.[server]`` extra (fastapi/uvicorn); skipped otherwise.
"""

import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

from tuple_space.space import TupleSpace

from aboard.board import Board
from aboard.http_space import HttpSpace
from aboard.models import COMPLETED, DEPENDENCY
from aboard.tui import BoardApp

try:
    import uvicorn
    from tuple_space.server import make_app
    HAVE_SERVER = True
except ImportError:
    HAVE_SERVER = False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(HAVE_SERVER, ".[server] extra (fastapi/uvicorn) not installed")
class HttpSpaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._envs = []

    def tearDown(self):
        for env in reversed(self._envs):
            self._stop_env(env)
        self._tmp.cleanup()

    def _new_env(self):
        space = TupleSpace(run_root=self._tmp.name)
        app = make_app(space)
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        app.state.on_server_exit = lambda: setattr(server, "should_exit", True)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                urllib.request.urlopen(f"{endpoint}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.05)
        else:
            self.fail("server did not come up")
        env = {"space": space, "endpoint": endpoint,
               "server": server, "thread": thread}
        self._envs.append(env)
        return env

    def _stop_env(self, env):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{env['endpoint']}/shutdown", method="POST"),
                timeout=5)
        except Exception:
            pass
        env["server"].should_exit = True
        env["thread"].join(timeout=10)
        env["space"].close()

    # ── surface parity ────────────────────────────────────────────────────

    def test_round_trip_primitives(self):
        env = self._new_env()
        h = HttpSpace(env["endpoint"])
        try:
            h.out("task", "t0", "hello", actor="alice")
            self.assertEqual(h.read_all("task", None, None),
                             [("task", "t0", "hello")])
            self.assertEqual(h.rdp("task", "t0"), ("task", "t0", "hello"))
            self.assertIsNone(h.rdp("task", "missing"))
            self.assertEqual(h.inp("task", "t0"), ("task", "t0", "hello"))
            self.assertEqual(h.read_all("task", None, None), [])
        finally:
            h.close()

    def test_board_insert_autogens_ids_over_the_wire(self):
        env = self._new_env()
        board = Board(HttpSpace(env["endpoint"]), actor="human")
        try:
            t0 = board.insert("task", instruction="first")
            t1 = board.insert("task", instruction="second")
            self.assertEqual((t0, t1), ("t0", "t1"))
            self.assertEqual(
                board.all("task"),
                [("task", "t0", "first"), ("task", "t1", "second")])
        finally:
            board.space.close()

    def test_are_dependencies_met_recomposed_over_the_wire(self):
        env = self._new_env()
        h = HttpSpace(env["endpoint"])
        try:
            # no dependency declared → ready
            self.assertTrue(h.are_dependencies_met("t1"))
            h.out(DEPENDENCY, "t1", ["t0"], actor="human")
            self.assertFalse(h.are_dependencies_met("t1"))
            h.out(COMPLETED, "t0", None, actor="human")
            self.assertTrue(h.are_dependencies_met("t1"))
        finally:
            h.close()

    def test_remote_space_has_no_local_run_dir(self):
        env = self._new_env()
        h = HttpSpace(env["endpoint"])
        try:
            self.assertIsNone(h.run_dir)
            self.assertTrue(getattr(h, "_remote", False))
        finally:
            h.close()

    # ── cross-process id autogen: no collisions across concurrent clients ──

    def test_concurrent_clients_get_unique_ids(self):
        env = self._new_env()
        # Seed the counters first (one client connects + inserts), then race
        # two clients: the counter tuple makes the reserve claim atomic.
        b0 = Board(HttpSpace(env["endpoint"]), actor="a")
        b0.insert("task", instruction="seed")
        b0.space.close()

        b1 = Board(HttpSpace(env["endpoint"]), actor="a")
        b2 = Board(HttpSpace(env["endpoint"]), actor="b")
        try:
            ids = []
            for i in range(20):
                ids.append(b1.insert("task", instruction=f"a{i}"))
                ids.append(b2.insert("task", instruction=f"b{i}"))
            # 40 inserts + 1 seed = 41 ids, all distinct, all autogen-shaped
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(ids), 40)
            self.assertNotIn("t0", ids)  # the seed claimed t0
        finally:
            b1.space.close()
            b2.space.close()

    def test_reset_refused_on_remote_board(self):
        import asyncio
        env = self._new_env()
        board = Board(HttpSpace(env["endpoint"]), actor="human")
        board.insert("task", instruction="keep me")

        async def run():
            app = BoardApp(board=board, human="human")
            toasts = []
            app.notify = lambda msg, **kw: toasts.append(msg)
            async with app.run_test() as pilot:
                await pilot.pause()
                app._run_command("/reset")
                await pilot.pause()
                self.assertTrue(any("disabled on a remote board" in t
                                    for t in toasts))
                self.assertEqual(board.all("task"),
                                 [("task", "t0", "keep me")])
        asyncio.run(run())
        board.space.close()


if __name__ == "__main__":
    unittest.main()
