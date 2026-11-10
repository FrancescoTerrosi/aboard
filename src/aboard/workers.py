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

Tier 2 additions (role-specialized, coordinated workers):

- **Role + skills.** A worker may carry a ``role`` (prompt guidance) and a
  ``skills`` list. A worker with skills claims only tasks that are untagged
  or tagged with skills it has; a generalist (no skills) claims anything.
- **Dependencies.** A worker claims a task only when its ``("dependency",
  tid, [deps])`` tuple is satisfied — every dependency has a ``("completed",
  dep, None)`` signal, which workers (and the planner) write on success.
"""

from __future__ import annotations

from typing import Callable, Optional

from tuple_space.llm_client import OpenAICompatibleClient

from .board import Board
from .models import COMPLETED, DEPENDENCY, TASK_SKILL
from .proposals import submit

__all__ = ["MAX_SUBTASKS", "MIN_SUBTASKS", "TaskWorker", "claim_task",
           "plan", "repost_task"]

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


def claim_task(space, tid: str, *, actor: str | None = None):
    """Atomically claim one task and detach its routing metadata.

    The task itself is claimed with ``inp`` (exactly-once); its skill tags and
    dependency tuple are drained so they don't orphan, and returned so the
    caller can restore them if the task is re-posted (the failure path).

    Returns ``(task, skills, deps)`` where ``task`` is the claimed tuple,
    ``skills`` the skill strings, and ``deps`` a list of dependency lists.
    Returns ``None`` when the task was already claimed by a peer.
    """
    task = space.inp("task", tid, None, actor=actor)
    if task is None:
        return None
    skills = []
    while True:
        s = space.inp(TASK_SKILL, tid, None, actor=actor)
        if s is None:
            break
        skills.append(s[2])
    deps = []
    while True:
        d = space.inp(DEPENDENCY, tid, None, actor=actor)
        if d is None:
            break
        deps.append(list(d[2]))
    return task, skills, deps


def repost_task(space, task, skills, deps, *, actor: str | None = None) -> None:
    """Re-post a claimed task and its metadata (no work lost on failure)."""
    space.out(*task, actor=actor)
    tid = task[1]
    for s in skills:
        space.out(TASK_SKILL, tid, s, actor=actor)
    for d in deps:
        space.out(DEPENDENCY, tid, list(d), actor=actor)


class TaskWorker:
    """One LLM worker: claim → execute → propose, until the board is empty.

    ``on_event(message)`` is called on every state change (claimed a task,
    proposed a result, finished or failed) — the TUI turns these into
    toasts and table refreshes. Inject ``client=`` with a scripted fake for
    deterministic tests (the LLM is the only seam).
    """

    def __init__(self, space, *, name: str, llm, client=None,
                 role: str | None = None, skills: list[str] | None = None):
        self.space = space
        self.name = name
        self.llm = llm
        self.role = role
        self.skills = list(skills or [])
        self.board = Board(space, actor=name)   # every op attributed to me
        self.client = client if client is not None else \
            OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)
        self.tokens_used = 0
        self.tasks_done = 0
        self._stopped = False

    def stop(self) -> None:
        """Stop before claiming new work (the in-flight task completes)."""
        self._stopped = True

    def _system_prompt(self) -> str:
        parts = [WORKER_PROMPT]
        if self.role:
            parts.append(f"Your role: {self.role}.")
        if self.skills:
            parts.append(f"Your skills: {', '.join(self.skills)}.")
        return " ".join(parts)

    def _matches_skill(self, tid: str) -> bool:
        """True when this worker may claim ``tid`` (skill routing)."""
        if not self.skills:
            return True  # generalist claims anything
        tags = self.space.read_all(TASK_SKILL, tid, None, actor=self.name)
        if not tags:
            return True  # untagged → any specialist may do it
        return all(t[2] in self.skills for t in tags)

    def _next_task(self):
        """The next claimable task (``(task, skills, deps)``) or ``None``.

        Scans pending tasks, skips those whose dependencies are unmet or whose
        skill tags this worker cannot satisfy, and claims the first eligible
        one by id. A task claimed by a peer between the scan and the take
        simply falls through to the next candidate.
        """
        for t in self.space.read_all("task", None, None, actor=self.name):
            tid = t[1]
            if not self.space.are_dependencies_met(tid):
                continue
            if not self._matches_skill(tid):
                continue
            claimed = claim_task(self.space, tid, actor=self.name)
            if claimed is not None:
                return claimed
        return None

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
            claimed = self._next_task()
            if claimed is None:
                remaining = len(self.space.read_all("task", None, None,
                                                    actor=self.name))
                if remaining:
                    msg = (f"{self.name}: no claimable tasks "
                           f"({remaining} pending — blocked by skills/deps)")
                else:
                    msg = f"{self.name}: no more tasks ({self.tasks_done} done)"
                if on_event:
                    on_event(msg)
                return
            task, skills, deps = claimed
            tid, instruction = task[1], task[2]
            try:
                result = await self._complete(self._system_prompt(), instruction)
            except Exception as e:
                # no work lost: re-post the task and its routing metadata
                repost_task(self.space, task, skills, deps, actor=self.name)
                if on_event:
                    on_event(f"{self.name}: task {tid} failed ({e}); re-posted")
                return
            pid = submit(self.space, "add", "knowledge",
                         {"name": f"result {tid}",
                          "context": f"task {tid}",
                          "text": result},
                         actor=self.name)
            # signal completion so dependent tasks become claimable
            self.space.out(COMPLETED, tid, None, actor=self.name)
            self.tasks_done += 1
            if on_event:
                on_event(f"{self.name}: task {tid} → proposal {pid}")


async def plan(space, *, tid: str, llm, client=None,
               actor: str | None = None) -> list[str]:
    """Decompose task ``tid`` into 2–5 subtasks (E5 planner semantics).

    The parent is claimed exactly-once (the take is the arbitration — no
    double-planning); subtasks are written as ``("task", "<tid>.k", text)``
    and attributed to ``actor``. On a failed or degenerate plan the parent
    is re-posted unchanged (metadata included) and the error re-raises — no
    work is lost. A successful plan marks the parent completed, so tasks that
    depend on it become claimable. Returns the subtask ids.
    """
    client = client if client is not None else \
        OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)
    claimed = claim_task(space, tid, actor=actor)
    if claimed is None:
        raise ValueError(f"no task {tid!r} on the board")
    task, skills, deps = claimed
    instruction = task[2]

    def _repost():
        repost_task(space, task, skills, deps, actor=actor)

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
    space.out(COMPLETED, tid, None, actor=actor)  # parent handled
    return ids
