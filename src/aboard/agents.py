"""ABoard named agents — role-specialized worker definitions in the space.

An agent is a tuple that describes what a worker should be when it starts::

    ("agent", name, role, [skills])

- ``name`` — the worker's name (WAL attribution, ``/agent start <name>``,
  ``/stop <name>``).
- ``role`` — a short role string injected into the worker's prompt.
- ``skills`` — an optional list of capabilities that gate task claims
  (skill routing; see ``workers.TaskWorker``). Empty = generalist.

Agents are defined but *idle* until started with ``/agent start <name>``.
Definitions persist in the space (mutations like any other), so a resumed
board keeps its agents. Skills are stored inside the agent tuple — this is
ABoard's capability registration; the substrate's ``out_capability`` /
``find_agents_with_skill`` remain available to other clients.
"""

from __future__ import annotations

from .models import AGENT

__all__ = ["add", "all_agents", "get"]


def add(space, name: str, role: str, skills=(), *, actor=None) -> str:
    """Define (or redefine) a named agent. Returns its name.

    Redefining an existing name replaces the old definition (take + write),
    so there is at most one definition per name.
    """
    if not name or not name.strip():
        raise ValueError("agent name is required")
    if not role or not role.strip():
        raise ValueError("agent role is required")
    skills = tuple(sorted(set(s.strip() for s in skills if s.strip())))
    space.inp(AGENT, name, None, None, actor=actor)
    space.out(AGENT, name, role.strip(), list(skills), actor=actor)
    return name


def get(space, name: str, *, actor=None):
    """The agent definition for ``name``, or ``None``."""
    return space.rdp(AGENT, name, None, None, actor=actor)


def all_agents(space, *, actor=None) -> list[tuple]:
    """Every defined agent, in insertion order."""
    return space.read_all(AGENT, None, None, None, actor=actor)
