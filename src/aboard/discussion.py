"""ABoard discussion — two named agents taking turns on the shared board.

Tier 3.2 (the free-form discussion sandbox): two ``GenericAgent``s converse
*through the tuple space*, alternating turns. There is no direct agent-to-agent
channel — each turn is an ``("agent_turn", name, text)`` tuple (the same
transcript 3.1 already writes), and the driver builds each agent's next turn
context from the *other* agent's recent turns. The "sandbox" is the observable
surface (a panel + toasts), not an isolated space: both agents share the real
board, so a turn that references or proposes board content is visible to the
human and to every other agent.

Turn-taking is the driver's job (``Discussion``), not the agents': the
substrate's agents never address each other. Stopping either agent (``/stop``)
halts the loop before that agent's next turn.
"""

from __future__ import annotations

from typing import Callable, Optional

from tuple_space import BudgetExceeded

from .llm_agents import GenericAgent
from .models import AGENT_TURN

__all__ = ["Discussion", "DEFAULT_ROUNDS"]

DEFAULT_ROUNDS = 3  # each agent speaks this many times (a, b, a, b, …)

DISCUSSANT_PROMPT = (
    "You are a discussant on a shared tuple space. Another named agent is "
    "taking turns with you; your turn context includes their recent words. "
    "Engage with their points, and be concise. You may read the board with "
    "your tuple tools before replying."
)


class Discussion:
    """Two named agents conversing turn-about through the tuple space.

    ``run(on_event)`` drives the alternation; each turn's context is the topic
    (opening) or the other agent's most recent turns. Inject ``client_a`` /
    ``client_b`` with scripted fakes for deterministic tests.
    """

    def __init__(self, space, *, a_name: str, b_name: str, llm,
                 topic: str = "", rounds: int = DEFAULT_ROUNDS,
                 client_a=None, client_b=None):
        self.space = space
        self.topic = topic
        self.rounds = max(1, rounds)
        self.a = GenericAgent(space, name=a_name, role="discussant", llm=llm,
                              system_prompt=DISCUSSANT_PROMPT, client=client_a)
        self.b = GenericAgent(space, name=b_name, role="discussant", llm=llm,
                              system_prompt=DISCUSSANT_PROMPT, client=client_b)
        self.agents = (self.a, self.b)

    def stop(self) -> None:
        """Stop the discussion (in-flight turn finishes)."""
        self.a.stop()
        self.b.stop()

    def _recent(self, name: str, n: int = 3) -> list[str]:
        return [t[2] for t in self.space.read_all(AGENT_TURN, name, None)][-n:]

    def _context(self, agent: GenericAgent, other: GenericAgent) -> str:
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
