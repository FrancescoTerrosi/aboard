"""ABoard workers — LLM agents that execute tasks from the board.

E5's work board, carried into ABoard:

- A **worker** claims a task atomically (``inp`` — exactly-once; the take is
  the arbitration), executes the instruction with the model, and proposes
  the result as a *knowledge add* — the human then approves it onto the
  board through the existing ``/proposals`` → ``/approve`` inbox. Workers
  never write directly: the board stays human-authored.
- The **planner** (``/plan``) decomposes a task into 2–5 concrete subtasks.
  The parent is claimed exactly-once (no double-planning), subtasks get ids
  ``<parent>.k`` — outside the autogen scheme (``next_id`` ignores them) —
  and workers claim them like any other task. A failed or empty plan
  re-posts the parent, so no work is lost.
- Every claim/proposal is attributed in the WAL (worker or approver); the
  transcript reads "worker claimed t0 at seq N, proposed p1 at seq M,
  human applied at seq K".

Cost control: a worker stops before claiming new work once its token budget
is crossed (the crossing task completes — a cut between a destructive claim
and its proposal would be lost work).
"""

from __future__ import annotations

from typing import Callable, Optional

from tuple_space.llm_client import OpenAICompatibleClient

from .board import Board
from .proposals import submit

__all__ = ["MAX_SUBTASKS", "MIN_SUBTASKS", "TaskWorker", "plan"]

MIN_SUBTASKS = 2
MAX_SUBTASKS = 5

WORKER_PROMPT = (
    "You are a worker agent on a shared task board. Execute the instruction "
    "concretely and completely. Reply with the work product itself — no "
    "preamble, no meta-commentary. Be concise."
)

PLANNER_PROMPT = (
    "Decompose the task below into 2-5 concrete, independently executable "
    "subtasks. Reply with ONE subtask per line: a short imperative "
    "instruction, no numbering, no bullets, no extra text."
)


class TaskWorker:
    """One LLM worker: claim → execute → propose, until the board is empty.

    ``on_event(message)`` is called on every state change (claimed a task,
    proposed a result, finished or failed) — the TUI turns these into
    toasts and table refreshes. Inject ``client=`` with a scripted fake for
    deterministic tests (the LLM is the only seam).
    """

    def __init__(self, space, *, name: str, llm, client=None):
        self.space = space
        self.name = name
        self.llm = llm
        self.board = Board(space, actor=name)   # every op attributed to me
        self.client = client if client is not None else \
            OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)
        self.tokens_used = 0
        self.tasks_done = 0
        self._stopped = False

    def stop(self) -> None:
        """Stop before claiming new work (the in-flight task completes)."""
        self._stopped = True

    async def _complete(self, system: str, user: str) -> str:
        response = await self.client.create(
            model=self.llm.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            timeout=120)
        self.tokens_used += getattr(response.usage, "total_tokens", 0)
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("empty completion")
        return content

    async def run(self, on_event: Optional[Callable[[str], None]] = None) -> None:
        """The worker loop: claim a task, execute it, propose the result."""
        while not self._stopped:
            if self.tokens_used > self.llm.token_budget:
                if on_event:
                    on_event(f"{self.name}: budget exceeded "
                             f"({self.tokens_used:,} tokens) — stopping")
                return
            t = self.space.inp("task", None, None, actor=self.name)  # exactly-once
            if t is None:
                if on_event:
                    on_event(f"{self.name}: no more tasks ({self.tasks_done} done)")
                return
            tid, instruction = t[1], t[2]
            try:
                result = await self._complete(WORKER_PROMPT, instruction)
            except Exception as e:
                # no work lost: the claimed task goes back on the board
                self.space.out("task", tid, instruction, actor=self.name)
                if on_event:
                    on_event(f"{self.name}: task {tid} failed ({e}); re-posted")
                return
            pid = submit(self.space, "add", "knowledge",
                         {"name": f"result {tid}",
                          "context": f"task {tid}",
                          "text": result},
                         actor=self.name)
            self.tasks_done += 1
            if on_event:
                on_event(f"{self.name}: task {tid} → proposal {pid}")


async def plan(space, *, tid: str, llm, client=None,
               actor: str | None = None) -> list[str]:
    """Decompose task ``tid`` into 2–5 subtasks (E5 planner semantics).

    The parent is claimed exactly-once (the take is the arbitration — no
    double-planning); subtasks are written as ``("task", "<tid>.k", text)``
    and attributed to ``actor``. On a failed or degenerate plan the parent
    is re-posted unchanged and the error re-raises — no work is lost.
    Returns the subtask ids.
    """
    client = client if client is not None else \
        OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)
    parent = space.inp("task", tid, None, actor=actor)
    if parent is None:
        raise ValueError(f"no task {tid!r} on the board")
    instruction = parent[2]

    def _repost():
        space.out("task", tid, instruction, actor=actor)  # no work lost

    try:
        response = await client.create(
            model=llm.model,
            messages=[{"role": "system", "content": PLANNER_PROMPT},
                      {"role": "user", "content": instruction}],
            timeout=120)
    except Exception as e:
        _repost()
        raise RuntimeError(f"planner failed: {e}") from e
    raw = (response.choices[0].message.content or "").strip()
    lines = [ln.strip().lstrip("-•0123456789. ") for ln in raw.splitlines()]
    subtasks = [ln for ln in lines if ln][:MAX_SUBTASKS]
    if len(subtasks) < MIN_SUBTASKS:
        _repost()
        raise RuntimeError(f"planner produced {len(subtasks)} subtask(s); "
                           f"{MIN_SUBTASKS}-{MAX_SUBTASKS} required")
    ids = [f"{tid}.{k}" for k in range(1, len(subtasks) + 1)]
    for sid, text in zip(ids, subtasks):
        space.out("task", sid, text, actor=actor)
    return ids