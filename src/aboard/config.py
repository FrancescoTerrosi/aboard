"""ABoard runtime configuration — stored in the space, resumed with it.

Runtime interaction policy — the chat agent's mode, the stock mount —
belongs to the *board*, not to a launch invocation. It is written by TUI
commands (``/mode``, ``/stock``) as ``("config", key, value)`` tuples:

- **Resume restores it.** Config tuples are mutations like any other, so
  the session replay that restores the board also restores its mode and
  stock mount — no second persistence mechanism.
- **Replace semantics = take + write** (the tuple-space update): exactly one
  config tuple per key at any time, while the WAL keeps every change
  (who switched what, attributed).
- Reads take the *latest* tuple, so even a racing duplicate write cannot
  hide a newer value.
- Config is not a curated tag: it never appears in the task/knowledge/skill
  views.

The launch CLI stays for identity/endpoint wiring only (``--name``,
``--model``, ``--run-root`` …).
"""

from __future__ import annotations

CONFIG = "config"
MODE = "mode"      # "human" | "auto" — the chat agent's write policy
STOCK = "stock"    # a runs root path — the read-only stock mount

__all__ = ["CONFIG", "MODE", "STOCK", "clear", "get", "store"]


def get(space, key: str, *, actor: str | None = None) -> str | None:
    """The latest stored value for ``key``, or ``None``."""
    tuples = space.read_all(CONFIG, key, None, actor=actor)
    return tuples[-1][2] if tuples else None


def store(space, key: str, value: str, *, actor: str | None = None) -> None:
    """Set ``key`` to ``value`` — take+write (the tuple-space update).

    At most one config tuple per key exists at any time; the WAL records
    the full change history (each take and each write, attributed).
    """
    space.inp(CONFIG, key, None, actor=actor)
    space.out(CONFIG, key, value, actor=actor)


def clear(space, key: str, *, actor: str | None = None):
    """Remove the key's config tuple, if present. Returns it or ``None``."""
    return space.inp(CONFIG, key, None, actor=actor)