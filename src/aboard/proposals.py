"""ABoard proposals — the agent's staging channel (human-driven mode).

A proposal is a tuple the chat agent writes when the human asks for a board
change that the agent is not allowed to apply directly::

    ("proposal", pid, kind, tag, values, source)

- ``pid``: auto-generated (``p0, p1, …`` — max index + 1, the same monotonic
  rule as the curated ids; survives restarts and deletions).
- ``kind``: ``"add"`` (``values`` = the item's content fields) or ``"claim"``
  (``values`` = ``{"id": …}``).
- ``values``: a JSON-safe dict, validated at submit time so every stored
  proposal is applyable.
- ``source``: optional JSON-safe provenance ``{"tag", "id"}`` — e.g. the task
  a worker executed. On apply, the approver records it as a
  ``("derived", new_id, tag, id)`` relation so provenance is queryable, not
  just prose.

Semantics (E5's work board, carried into ABoard): the agent proposes, the
human decides. Deciding *takes* the proposal atomically (``inp``), so a
proposal gets exactly one decision even with two humans at the board. The
apply itself goes through the curated Board and is attributed to the
approver — the WAL records the whole story (agent proposed at seq N, human
decided at seq M). Proposals are mutations like any other, so pending ones
survive restarts via session persistence.
"""

from __future__ import annotations

from typing import Optional

from .models import DERIVED, fields, key_field, reserve_id, spec

__all__ = ["ADD", "CLAIM", "PROPOSAL", "apply", "describe", "pending",
           "reject", "submit"]

PROPOSAL = "proposal"
ADD = "add"
CLAIM = "claim"


def submit(space, kind: str, tag: str, values: dict, *, actor: str,
           source: Optional[dict] = None) -> str:
    """Write one proposal; returns its pid.

    Validates kind/tag/values so that every *stored* proposal is applyable —
    an apply-time failure would orphan a proposal the decision already took.
    ``source`` is optional provenance (``{"tag": "task", "id": "t0.1"}``),
    recorded as a ``derived`` relation when the proposal is applied.
    """
    if kind not in (ADD, CLAIM):
        raise ValueError(f"unknown proposal kind {kind!r} (expected add/claim)")
    spec(tag)  # raises on non-curated tags
    if not isinstance(values, dict):
        raise ValueError("values must be a dict")
    if kind == ADD:
        unknown = set(values) - set(fields(tag))
        if unknown:
            raise ValueError(f"unknown field(s) for {tag!r}: {sorted(unknown)}")
        full = {f: values.get(f, "") for f in fields(tag)}
        for f, v in full.items():
            if not isinstance(v, str):
                raise ValueError(f"{f} must be a str, got {type(v).__name__}")
        if not full[key_field(tag)].strip():
            raise ValueError(f"{key_field(tag)!r} is required for {tag!r}")
    else:  # CLAIM
        tid = values.get("id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("claim proposals need a non-empty 'id'")

    if source is not None:
        if not isinstance(source, dict) or not isinstance(source.get("tag"), str) \
                or not source["tag"] or not isinstance(source.get("id"), str) \
                or not source["id"]:
            raise ValueError("source must be {'tag': str, 'id': str}")

    pid = reserve_id(space, PROPOSAL, "p", actor=actor)
    space.out(PROPOSAL, pid, kind, tag, dict(values), source, actor=actor)
    return pid


def pending(space) -> list[tuple]:
    """Every undecided proposal, oldest first."""
    return space.read_all(PROPOSAL, None, None, None, None)


def _take(space, pid: Optional[str], *, actor) -> Optional[tuple]:
    """Claim the decision on one proposal (atomic; exactly one decision)."""
    if pid is not None:
        return space.inp(PROPOSAL, pid, None, None, None, actor=actor)
    for t in pending(space):  # oldest first
        got = space.inp(PROPOSAL, t[1], None, None, None, actor=actor)
        if got is not None:
            return got
    return None


def apply(space, board, pid: Optional[str] = None, *,
          actor=None) -> str:
    """Decide one proposal and apply it through the Board (attributed to the
    approver). ``board`` is the human's Board — the agent never applies.
    Provenance (``source``) is recorded as a ``derived`` relation after the
    item lands, so the link is queryable."""
    t = _take(space, pid, actor=actor)
    if t is None:
        return f"no {'proposal ' + pid if pid else 'pending proposals'}"
    kind, tag, values = t[2], t[3], t[4]
    source = t[5] if len(t) > 5 else None  # old 5-field proposals: no source
    if kind == ADD:
        tid = board.insert(tag, **values)
        if source:
            space.out(DERIVED, tid, source["tag"], source["id"], actor=actor)
        return f"{t[1]}: added {tag} {tid}"
    if kind == CLAIM:
        got = board.remove(tag, values["id"])
        return (f"{t[1]}: claimed {tag} {values['id']}" if got
                else f"{t[1]}: no {tag} {values['id']} on the board")
    return f"{t[1]}: error: unknown kind {kind!r}"  # unreachable (validated)


def reject(space, pid: Optional[str] = None, *, actor=None) -> str:
    """Decide one proposal by taking it and applying nothing."""
    t = _take(space, pid, actor=actor)
    if t is None:
        return f"no {'proposal ' + pid if pid else 'pending proposals'}"
    return f"{t[1]}: rejected"


def describe(t: tuple) -> str:
    """One-line human-readable rendering (the /proposals view, tool results)."""
    pid, kind, tag, values = t[1], t[2], t[3], t[4]
    source = t[5] if len(t) > 5 else None
    if kind == ADD:
        preview = ", ".join(f"{k}={v!r}" for k, v in values.items())
        src = f" ← {source['tag']} {source['id']}" if source else ""
        return f"{pid}: add {tag} ({preview}){src}"
    return f"{pid}: claim {tag} {values.get('id')}"