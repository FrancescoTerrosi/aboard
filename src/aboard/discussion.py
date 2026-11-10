"""ABoard discussion — two named agents taking turns on the shared board.

Tier 3.2 (the free-form discussion sandbox): two discussants converse
*through the tuple space*, alternating turns. There is no direct agent-to-agent
channel — each turn is an ``("agent_turn", name, text)`` tuple (the same
transcript 3.1 already writes), and the driver builds each agent's next turn
context from the *other* agent's recent turns. The "sandbox" is the observable
surface (a panel + toasts), not an isolated space: both agents share the real
board, so their turns are visible to the human and to every other agent.

A discussant is a **plain text completion**, not the substrate's tool-calling
``LLMAgent``. That is deliberate: a discussion turn is prose-in → prose-out;
giving a discussant tuple tools lets the model wander off reading the board
(multi-round tool loops, large context) and return no prose — which reads as an
empty turn and kills the conversation. The driver is the one that reads the
board (to build context); the discussants only speak.

Turn-taking is the driver's job (``Discussion``), not the agents': the
substrate's agents never address each other. Stopping either agent (``/stop``)
halts the loop before that agent's next turn.
"""

from __future__ import annotations

from typing import Callable, Optional

from tuple_space import BudgetExceeded
from tuple_space.llm_client import OpenAICompatibleClient

from .models import AGENT_TURN

__all__ = ["Discussion", "Discussant", "DEFAULT_ROUNDS"]

DEFAULT_ROUNDS = 3  # each discussant speaks this many times (a, b, a, b, …)

DISCUSSANT_PROMPT = (
    "You are a discussant on a shared tuple space. Another named agent is "
    "taking turns with you; your turn context includes the topic and their "
    "recent words. Engage with their points and reply in prose only — be "
    "concise and do not address the other agent directly."
)


class Discussant:
    """One prose-only discussant.

    ``step(context)`` is a single plain completion (no tuple tools): the
    context is the topic + the other's recent turns, the reply is recorded as
    an ``agent_turn`` tuple. Tracks budget with the substrate's step-boundary
    rule (the crossing step completes; the next step raises ``BudgetExceeded``).
    Inject ``client=`` with a scripted fake for deterministic tests.
    """

    def __init__(self, space, *, name: str, llm,
                 system_prompt: str = DISCUSSANT_PROMPT, client=None):
        self.space = space
        self.name = name
        self.role = "discussant"
        self.skills: list[str] = []
        self.llm = llm
        self.client = client if client is not None else \
            OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)
        self.tokens_used = 0
        self.api_calls = 0
        self.turns = 0
        self._stopped = False
        self._budget_exceeded = False
        self._system_prompt = system_prompt

    def stop(self) -> None:
        self._stopped = True

    async def step(self, context: str = "") -> str | None:
        """One prose turn; returns the reply (or ``None`` when empty)."""
        if self._budget_exceeded:
            raise BudgetExceeded(
                f"discussant {self.name} exceeded token budget: "
                f"{self.tokens_used:,} / {self.llm.token_budget:,}"
            )
        response = await self.client.create(
            model=self.llm.model,
            messages=[{"role": "system", "content": self._system_prompt},
                      {"role": "user", "content": context}],
            timeout=120)
        self.api_calls += 1
        total = getattr(response.usage, "total_tokens", 0)
        self.tokens_used += total
        if self.tokens_used > self.llm.token_budget:
            self._budget_exceeded = True
        text = (response.choices[0].message.content or "").strip()
        if text:
            self.space.out(AGENT_TURN, self.name, text, actor=self.name)
            self.turns += 1
        return text or None


class Discussion:
    """Two named discussants conversing turn-about through the tuple space.

    ``run(on_event)`` drives the alternation; each turn's context is the topic
    (opening) or the other discussant's most recent turns. Inject ``client_a`` /
    ``client_b`` with scripted fakes for deterministic tests.
    """

    def __init__(self, space, *, a_name: str, b_name: str, llm,
                 topic: str = "", rounds: int = DEFAULT_ROUNDS,
                 actor: str | None = None, client_a=None, client_b=None):
        self.space = space
        self.topic = topic
        self.rounds = max(1, rounds)
        self.actor = actor
        self.a = Discussant(space, name=a_name, llm=llm, client=client_a)
        self.b = Discussant(space, name=b_name, llm=llm, client=client_b)
        self.agents = (self.a, self.b)

    def stop(self) -> None:
        """Stop the discussion (in-flight turn finishes)."""
        self.a.stop()
        self.b.stop()

    def _recent(self, name: str, n: int = 3) -> list[str]:
        # The driver's context reads are attributed to the discussion's actor
        # (the human who started it), matching every other read in ABoard.
        return [t[2] for t in self.space.read_all(AGENT_TURN, name, None,
                                                  actor=self.actor)][-n:]

    def _context(self, agent: Discussant, other: Discussant) -> str:
        """Build one turn's context: topic (opening) or the other's words."""
        parts = []
        if self.topic:
            parts.append(f"Topic: {self.topic}")
        recent = self._recent(other.name)
        if recent:
            parts.append(f"{other.name} said:\n" +
                         "\n".join(f"- {t}" for t in recent))
            parts.append(f"Respond as {agent.name}.")
        else:
            parts.append(f"You open the discussion as {agent.name}.")
        return "\n\n".join(parts)

    async def run(self, on_event: Optional[Callable[[str], None]] = None) -> None:
        """Alternate turns until stopped, a turn comes back empty, budget is
        crossed, or ``rounds`` is exhausted."""
        for i in range(self.rounds * 2):
            agent = self.agents[i % 2]
            if agent._stopped:
                break
            other = self.agents[(i + 1) % 2]
            try:
                text = await agent.step(self._context(agent, other))
            except BudgetExceeded as e:
                if on_event:
                    on_event(f"{agent.name}: {e}")
                break
            except Exception as e:
                if on_event:
                    on_event(f"{agent.name}: failed ({e})")
                break
            if text and on_event:
                on_event(f"{agent.name}: {text[:60]}")
            if not text:
                break
        if on_event:
            on_event(f"discussion ended ({self.a.name}: {self.a.turns} turn(s), "
                     f"{self.b.name}: {self.b.turns} turn(s))")
