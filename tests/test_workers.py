"""Workers & planner — deterministic tests through the client seam.

The E5 properties, read from the tuple-space state and the WAL:
exactly-once claims, results proposed for human approval (workers never
apply), no work lost on failure, the parent re-planned only once.
"""

import asyncio
import json
import tempfile
import unittest

from tuple_space import TupleSpace
from tuple_space.llm_client import OpenAICompatibleClient

from aboard.chat import LLMConfig
from aboard.workers import TaskWorker, claim_task, plan, repost_task
from tests_support import ScriptedClient, completion


def text_completion(content, total=10):
    """A plain (non-tool) completion — the worker/planner call shape."""
    return completion(content=content, total=total)


class WorkerTest(unittest.TestCase):
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

    def test_claims_executes_proposes_until_empty(self):
        self.space.out("task", "t0", "do A", actor="human")
        self.space.out("task", "t1", "do B", actor="human")
        w = TaskWorker(self.space, name="w0", llm=self.llm,
                       client=ScriptedClient(text_completion("result A"),
                                             text_completion("result B")))
        events = []
        asyncio.run(w.run(on_event=events.append))
        # exactly-once: every task claimed, none left, none duplicated
        self.assertEqual(self.space.read_all("task", None, None), [])
        self.assertEqual(w.tasks_done, 2)
        # results are PROPOSALS (knowledge adds), attributed to the worker
        props = self.space.read_all("proposal", None, None, None, None)
        self.assertEqual([(p[1], p[2], p[3]) for p in props],
                         [("p0", "add", "knowledge"), ("p1", "add", "knowledge")])
        self.assertEqual(props[0][4]["name"], "result t0")
        self.assertEqual(props[0][4]["text"], "result A")
        muts = [r for r in self._records() if r["kind"] == "mutation"]
        self.assertIn("w0", {r["actor"] for r in muts})
        self.assertIn("no more tasks", events[-1])

    def test_two_workers_claim_exactly_once(self):
        for i in range(3):
            self.space.out("task", f"t{i}", f"do {i}", actor="human")
        w0 = TaskWorker(self.space, name="w0", llm=self.llm,
                        client=ScriptedClient(*[text_completion(f"r{i}")
                                                for i in range(3)]))
        w1 = TaskWorker(self.space, name="w1", llm=self.llm,
                        client=ScriptedClient(*[text_completion(f"r{i}")
                                                for i in range(3)]))
        async def run_both():
            await asyncio.gather(w0.run(), w1.run())
        asyncio.run(run_both())
        self.assertEqual(self.space.read_all("task", None, None), [])   # no phantoms
        self.assertEqual(w0.tasks_done + w1.tasks_done, 3)              # none lost
        self.assertEqual(len(self.space.read_all("proposal", None, None, None, None)),
                         3)                                            # none duplicated

    def test_failure_reposts_the_claimed_task(self):
        self.space.out("task", "t0", "important work", actor="human")
        w = TaskWorker(self.space, name="w0", llm=self.llm,
                       client=ScriptedClient(RuntimeError("endpoint down")))
        events = []
        asyncio.run(w.run(on_event=events.append))
        # no work lost: the task is back, unchanged, attributed
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t0", "important work")])
        self.assertTrue(any("re-posted" in e for e in events))

    def test_budget_stop_happens_before_claiming_new_work(self):
        self.space.out("task", "t0", "do A", actor="human")
        self.space.out("task", "t1", "do B", actor="human")
        w = TaskWorker(self.space, name="w0", llm=LLMConfig(token_budget=50),
                       client=ScriptedClient(text_completion("result A", total=100),
                                             text_completion("never reached")))
        events = []
        asyncio.run(w.run(on_event=events.append))
        self.assertEqual(w.tasks_done, 1)          # the crossing task completes
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t1", "do B")])  # unclaimed — never lost

    def test_stop_flag(self):
        w = TaskWorker(self.space, name="w0", llm=self.llm,
                       client=ScriptedClient())
        w.stop()
        asyncio.run(w.run())
        self.assertEqual(w.tasks_done, 0)

    def test_role_and_skills_shape_the_prompt(self):
        w = TaskWorker(self.space, name="alice", llm=self.llm,
                       role="writer", skills=["writing", "editing"])
        prompt = w._system_prompt()
        self.assertIn("writer", prompt)
        self.assertIn("writing", prompt)
        self.assertIn("editing", prompt)

    def test_skill_routing_claims_matching_and_skips_others(self):
        from aboard.models import TASK_SKILL
        self.space.out("task", "t0", "write copy", actor="human")
        self.space.out(TASK_SKILL, "t0", "writing", actor="human")
        self.space.out("task", "t1", "fix bug", actor="human")
        self.space.out(TASK_SKILL, "t1", "coding", actor="human")
        self.space.out("task", "t2", "general task", actor="human")  # untagged
        w = TaskWorker(self.space, name="writer", llm=self.llm,
                       skills=["writing"],
                       client=ScriptedClient(text_completion("r0"),
                                             text_completion("r2")))
        asyncio.run(w.run())
        # matching (t0) and untagged (t2) claimed; coding (t1) left for a peer
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t1", "fix bug")])
        self.assertEqual(w.tasks_done, 2)

    def test_dependency_blocks_until_completed(self):
        from aboard.models import COMPLETED, DEPENDENCY
        self.space.out("task", "t0", "first", actor="human")
        self.space.out("task", "t1", "second", actor="human")
        self.space.out(DEPENDENCY, "t1", ["t0"], actor="human")
        w = TaskWorker(self.space, name="w0", llm=self.llm,
                       client=ScriptedClient(text_completion("r0"),
                                             text_completion("r1")))
        asyncio.run(w.run())
        self.assertEqual(w.tasks_done, 2)
        self.assertEqual(self.space.read_all("task", None, None), [])
        self.assertEqual(self.space.read_all(COMPLETED, None, None),
                         [("completed", "t0", None), ("completed", "t1", None)])

    def test_dependency_blocked_task_not_claimed(self):
        from aboard.models import DEPENDENCY
        self.space.out("task", "t1", "do later", actor="human")
        self.space.out(DEPENDENCY, "t1", ["t0"], actor="human")  # t0 never done
        w = TaskWorker(self.space, name="w0", llm=self.llm,
                       client=ScriptedClient())
        events = []
        asyncio.run(w.run(on_event=events.append))
        self.assertEqual(w.tasks_done, 0)
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t1", "do later")])
        self.assertTrue(any("no claimable tasks" in e for e in events))

    def test_claim_task_detaches_and_repost_restores(self):
        from aboard.models import DEPENDENCY, TASK_SKILL
        self.space.out("task", "t0", "do", actor="human")
        self.space.out(TASK_SKILL, "t0", "writing", actor="human")
        self.space.out(DEPENDENCY, "t0", ["tx"], actor="human")
        claimed = claim_task(self.space, "t0", actor="w0")
        self.assertIsNotNone(claimed)
        task, skills, deps = claimed
        self.assertEqual(task, ("task", "t0", "do"))
        self.assertEqual(skills, ["writing"])
        self.assertEqual(deps, [["tx"]])
        # metadata detached with the task
        self.assertEqual(self.space.read_all("task", None, None), [])
        self.assertEqual(self.space.read_all(TASK_SKILL, None, None), [])
        self.assertEqual(self.space.read_all(DEPENDENCY, None, None), [])
        # re-post restores everything
        repost_task(self.space, task, skills, deps, actor="w0")
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t0", "do")])
        self.assertEqual(self.space.read_all(TASK_SKILL, None, None),
                         [("task_skill", "t0", "writing")])
        self.assertEqual(self.space.read_all(DEPENDENCY, None, None),
                         [("dependency", "t0", ["tx"])])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.space = TupleSpace(run_root=self._tmp.name)
        self.llm = LLMConfig(model="test-model")

    def tearDown(self):
        self.space.close()
        self._tmp.cleanup()

    def test_decomposes_exactly_once_with_subtask_ids(self):
        self.space.out("task", "t0", "build a website", actor="human")
        ids = asyncio.run(plan(self.space, tid="t0", llm=self.llm,
                               client=ScriptedClient(text_completion(
                                   "design the pages\nwrite the copy\n"
                                   "implement the frontend")),
                               actor="human"))
        self.assertEqual(ids, ["t0.1", "t0.2", "t0.3"])
        # the parent is gone (claimed exactly-once), subtasks are tasks
        self.assertEqual(self.space.read_all("task", None, None), [
            ("task", "t0.1", "design the pages"),
            ("task", "t0.2", "write the copy"),
            ("task", "t0.3", "implement the frontend"),
        ])
        # subtask ids ("t0.1") are outside the autogen scheme — next_id
        # ignores them, and the claimed parent's index is freely reusable
        # (distinct ids, no collision)
        from aboard.models import next_id
        self.assertEqual(next_id("task", self.space.read_all("task", None, None)),
                         "t0")
        # the parent is marked completed, so dependents become claimable
        from aboard.models import COMPLETED
        self.assertEqual(self.space.read_all(COMPLETED, None, None),
                         [("completed", "t0", None)])

    def test_failed_plan_reposts_the_parent(self):
        self.space.out("task", "t0", "build a website", actor="human")
        with self.assertRaises(RuntimeError):
            asyncio.run(plan(self.space, tid="t0", llm=self.llm,
                             client=ScriptedClient(RuntimeError("endpoint down")),
                             actor="human"))
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t0", "build a website")])  # no work lost

    def test_degenerate_plan_reposts_and_cleans_numbering(self):
        self.space.out("task", "t0", "trivial", actor="human")
        with self.assertRaises(RuntimeError):
            asyncio.run(plan(self.space, tid="t0", llm=self.llm,
                             client=ScriptedClient(text_completion("only one thing")),
                             actor="human"))
        self.assertEqual(self.space.read_all("task", None, None),
                         [("task", "t0", "trivial")])

    def test_missing_task_raises(self):
        with self.assertRaises(ValueError):
            asyncio.run(plan(self.space, tid="t99", llm=self.llm,
                             client=ScriptedClient()))


if __name__ == "__main__":
    unittest.main()