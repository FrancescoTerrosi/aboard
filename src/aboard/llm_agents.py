"""ABoard generic agents — arbitrary ``LLMAgent``s coordinating on any tuples.

Tier 3.1: unlike the curated ``TaskWorker`` (task-queue claim → execute →
propose), a generic agent is the substrate's ``LLMAgent`` — it reasons and
uses the raw Linda tools (``out``/``rdp``/``inp``/``in_``/``read_all``) over
the *whole* board, any tag, not just task/knowledge/skill. The TUI spawns one
with a name, role, skills, and an optional goal; it steps until stopped, out
of budget, or out of steps.

Live observability comes from the board itself:

- **Transcript** — each turn's prose is written as a ``("agent_turn", name,
  text)`` tuple, so the transcript is part of the space (survives restarts,
  visible to every other agent).
- **Budget** — the substrate tracks ``tokens_used`` / ``token_budget`` and
  raises ``BudgetExceeded`` at the next step boundary (the crossing step
  completes).
- **Skills** — registered via the substrate's ``out_capability`` on the
  agent's first step (in-process boards only; see the substrate's docs).

The client seam: the substrate builds its own ``OpenAICompatibleClient``, but
``agent.client`` is the only transport boundary, so tests swap in a scripted
client by reassigning it. No substrate change.
"""

from __future__ import annotations

from typing import Callable, Optional

from tuple_space import BudgetExceeded, LLMAgent

from .models import AGENT_TURN

__all__ = ["GenericAgent", "MAX_STEPS", "GENERIC_PROMPT"]

MAX_STEPS = 20


GENERIC_PROMPT = (
    "You are a coordination agent on a shared tuple space. Use your tuple "
    "tools to read the board, reason, and write your conclusions back as "
    "tuples. Be concise; cite the tuples you relied on."
)


class GenericAgent:
    """One substrate ``LLMAgent`` running on the shared board.

    ``run(on_event)`` steps the agent until stopped, out of budget, or out of
    steps, writing each non-empty turn as an ``agent_turn`` tuple. Inject
    ``client=`` with a scripted fake for deterministic tests (reassigns the
    substrate agent's transport seam).
    """

    def __init__(self, space, *, name: str, role: str, llm,
                 skills: list[str] | None = None, goal: str | None = None,
                 system_prompt: str | None = None, client=None):
        self.space = space
        self.name = name
        self.role = role
        self.llm = llm
        self.skills = list(skills or [])
        self.goal = goal
        self.tokens_used = 0
        self.api_calls = 0
        self.turns = 0
        self._stopped = False

        prompt = system_prompt or GENERIC_PROMPT
        if goal:
            prompt += f"\nYour goal: {goal}."
        self._agent = LLMAgent(
            name, role, space=space, model=llm.model,
            base_url=llm.base_url, api_key=llm.api_key,
            token_budget=llm.token_budget, skills=self.skills,
            system_prompt=prompt,
        )
        if client is not None:
            self._agent.client = client  # the only transport seam

    def stop(self) -> None:
        self._stopped = True

    def _context(self) -> str:
        # The substrate deliberately does NOT inject a snapshot (the agent
        # discovers state with its tools). A goal, when given, is the step
        # context; otherwise the agent gets the substrate's default nudge.
        return self.goal or ""

    async def step(self, context: str = "") -> str | None:
        """One reasoning step: run the substrate agent, record the turn.

        Returns the (stripped) content, or ``None`` when the model produced
        nothing. Raises ``BudgetExceeded`` at the next step boundary once the
        budget is crossed (the substrate's rule).
        """
        result = await self._agent.step(context=context)
        self.tokens_used = self._agent.tokens_used
        self.api_calls = self._agent.api_calls
        text = (result.get("content") or "").strip()
        # The substrate maps an empty assistant message to this sentinel; for
        # a transcript it means "no prose this turn" — not a real turn.
        if text == "(tool calls only)":
            text = ""
        if text:
            self.space.out(AGENT_TURN, self.name, text, actor=self.name)
            self.turns += 1
        return text or None

    async def run(self, on_event: Optional[Callable[[str], None]] = None,
                  *, max_steps: int = MAX_STEPS) -> None:
        """Step until stopped / budget / step limit, recording each turn."""
        while not self._stopped and self.turns < max_steps:
            try:
                text = await self.step(self._context())
            except BudgetExceeded as e:
                if on_event:
                    on_event(f"{self.name}: {e}")
                return
            except Exception as e:
                if on_event:
                    on_event(f"{self.name}: failed ({e})")
                return
            if text and on_event:
                on_event(f"{self.name}: {text[:60]}")
        if on_event:
            self._done(on_event)

    def _done(self, on_event) -> None:
        reason = "stopped" if self._stopped else "step limit reached"
        on_event(f"{self.name}: {reason} ({self.turns} turn(s), "
                 f"{self.tokens_used:,} tokens)")
