"""Config tuples — runtime policy stored in the space, resumed with it.

The key property: config rides the same replay as everything else. No
second persistence mechanism; the board remembers its own mode.
"""

import json
import tempfile
import unittest

from tuple_space import TupleSpace

from aboard import config
from aboard.session import open_space


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.space = TupleSpace(run_root=self.root)

    def tearDown(self):
        if not self.space._closed:
            self.space.close()
        self._tmp.cleanup()

    def _records(self):
        self.space.flush_log()
        with open(self.space.ops_jsonl_path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_default_is_none(self):
        self.assertIsNone(config.get(self.space, config.MODE))

    def test_store_and_get(self):
        config.store(self.space, config.MODE, "auto", actor="human")
        self.assertEqual(config.get(self.space, config.MODE), "auto")

    def test_store_replaces_exactly_one_tuple(self):
        config.store(self.space, config.MODE, "human", actor="human")
        config.store(self.space, config.MODE, "auto", actor="human")
        tuples = self.space.read_all(config.CONFIG, config.MODE, None)
        self.assertEqual(tuples, [("config", "mode", "auto")])  # one, latest
        # but the WAL keeps the whole change history, attributed (the first
        # store's empty take is logged too — every take is)
        ops = [(r["op"], r["actor"]) for r in self._records()
               if r["args"][:1] == ["config"]]
        self.assertEqual(ops.count(("INP", "human")), 2)
        self.assertEqual(ops.count(("OUT", "human")), 2)

    def test_clear(self):
        config.store(self.space, config.STOCK, "/x", actor="human")
        self.assertIsNotNone(config.clear(self.space, config.STOCK,
                                           actor="human"))
        self.assertIsNone(config.get(self.space, config.STOCK))
        self.assertIsNone(config.clear(self.space, config.STOCK,
                                       actor="human"))  # already gone

    def test_rides_session_persistence(self):
        # THE property: a resumed board remembers its own mode — the same
        # replay that restores the board restores its config, no extra file.
        config.store(self.space, config.MODE, "auto", actor="human")
        config.store(self.space, config.STOCK, "/stock/root", actor="human")
        self.space.close()
        space, _ = open_space(self.root)
        try:
            self.assertEqual(config.get(space, config.MODE), "auto")
            self.assertEqual(config.get(space, config.STOCK), "/stock/root")
        finally:
            space.close()


if __name__ == "__main__":
    unittest.main()