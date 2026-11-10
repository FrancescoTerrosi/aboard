"""ABoard data models — the curated tuple conventions: task, knowledge, skill.

These are NOT substrate changes. The tuple space stays generic; ABoard
defines three *templates* on top of the generic ``out``/``rdp`` primitives::

    ("task",      t<N>,     instruction)
    ("knowledge", know<N>,  name, context, text)
    ("skill",     skill<N>, name, context, text)

- ``id`` is auto-generated per tag (``t0``, ``know0``, ``skill0``, …),
  derived from the board's existing tuples (max index + 1), so it survives
  restarts *and* deletions without collision.
- Each tag declares its own content fields. Search locks any of them (exact
  match) and leaves the rest as wildcards (``None``) — the substrate's
  associative matching. The first field is the "key" (required, non-empty);
  the rest are optional detail.
"""

from __future__ import annotations

import re
import threading
from typing import NamedTuple


class TagSpec(NamedTuple):
    """A curated tag: its id prefix and its content fields (after tag + id)."""
    id_prefix: str
    fields: tuple[str, ...]


TASK = "task"
KNOWLEDGE = "knowledge"
SKILL = "skill"

# The three curated tags. ``task`` keeps the substrate's existing
# ``("task", id, instruction)`` shape; knowledge/skill add name/context/text.
TAG_SPECS = {
    TASK: TagSpec("t", ("instruction",)),
    KNOWLEDGE: TagSpec("know", ("name", "context", "text")),
    SKILL: TagSpec("skill", ("name", "context", "text")),
}
TAGS = tuple(TAG_SPECS)

# Internal counter tag for atomic id reservation (see ``reserve_id``). It is
# not a curated tag: it never appears in the task/knowledge/skill views and is
# drained by ``session.reset`` so ids restart, matching the scan behavior it
# replaces.
COUNTER_TAG = "id_counter"

# Orchestration / bookkeeping tags — ABoard-level conventions that are NOT
# curated board tags (they never appear in the task/knowledge/skill views).
AGENT = "agent"            # ("agent", name, role, [skills]) — named worker def
TASK_SKILL = "task_skill"  # ("task_skill", task_id, skill) — required skill
DEPENDENCY = "dependency"  # ("dependency", task_id, [deps]) — task dependencies
COMPLETED = "completed"    # ("completed", task_id, None) — completion signal

# Tags that are infrastructure, not board content: they are excluded from the
# "resumed N item(s)" count and never make a run non-empty on their own.
BOOKKEEPING_TAGS = frozenset({COUNTER_TAG, COMPLETED})

# Everything a factory reset must drain: curated content, chat/proposals/config,
# orchestration metadata, and bookkeeping.
RESET_TAGS = frozenset({*TAGS, "chat", "proposal", "config", AGENT,
                        TASK_SKILL, DEPENDENCY, COUNTER_TAG, COMPLETED})

# Per-process lock serializing id reservation. It makes ``reserve_id`` correct
# under concurrent in-process writers (threads, the TUI's worker coroutines).
# Cross-process atomicity relies on the counter tuple's ``inp`` claim; the
# *seed* step is the remaining cross-process gap (see ``reserve_id``).
_ID_LOCK = threading.Lock()

_ID_RE = re.compile(r"^([a-z]+)(\d+)$")


def spec(tag: str) -> TagSpec:
    """The :class:`TagSpec` for a curated tag (raises on unknown tags)."""
    try:
        return TAG_SPECS[tag]
    except KeyError:
        raise ValueError(f"unknown tag {tag!r}; ABoard curates {TAGS}") from None


def id_prefix(tag: str) -> str:
    return spec(tag).id_prefix


def fields(tag: str) -> tuple[str, ...]:
    """The content-field names for a tag (in tuple order, after tag + id)."""
    return spec(tag).fields


def key_field(tag: str) -> str:
    """The required, non-empty field (first content field)."""
    return spec(tag).fields[0]


def parse_index(tid: str) -> int | None:
    """Extract the numeric index from an id like ``know7`` -> ``7``.

    Returns ``None`` for ids that don't match the scheme (e.g. a hand-written
    id), which are then simply ignored when computing the next id.
    """
    m = _ID_RE.match(tid or "")
    return int(m.group(2)) if m else None


def _max_index(tag: str, tuples) -> int:
    """The largest numeric index among ``tuples`` of ``tag`` (``-1`` if none).

    Non-conforming ids (e.g. hand-written ``"know"``, subtask ids like
    ``"t0.1"``) are ignored, exactly as ``next_id`` has always done.
    """
    max_idx = -1
    for t in tuples:
        if t[0] == tag:
            idx = parse_index(t[1])
            if idx is not None:
                max_idx = max(max_idx, idx)
    return max_idx


def next_id(tag: str, tuples) -> str:
    """Next auto id for ``tag``: one past the max existing index.

    Scans the board's existing tuples of that tag, takes the largest numeric
    index, and returns the next one — monotonic across deletions, since a
    removed ``know5`` simply leaves ``know6`` free rather than being reused.

    This is the pure/scan form (no tuple space): used for id *computation*
    only. Callers that need an atomically *reserved* id (no collision under
    concurrent writers) should use :func:`reserve_id`.
    """
    return f"{id_prefix(tag)}{_max_index(tag, tuples) + 1}"


def reserve_id(space, tag: str, prefix: str, *, actor=None) -> str:
    """Atomically reserve the next id for ``tag`` via a per-tag counter tuple.

    Ids come from a single counter tuple per tag::

        ("id_counter", tag, next_index)

    ``inp`` takes the counter (exactly-once — the tuple space arbitrates), the
    index is incremented, and the counter is written back. ``prefix`` is the
    id's textual prefix (``id_prefix(tag)`` for curated tags, ``"p"`` for
    proposals); ``tag`` is both the counter key and the tuple tag scanned on
    first use.

    **First use** (no counter yet) seeds the counter from a scan of the
    existing tuples of ``tag``. The seed and the claim are both serialized by
    ``_ID_LOCK``, so ids are unique under concurrent in-process writers
    (threads, the TUI's worker coroutines) — the pre-counter read-then-write
    could collide; this cannot.

    **Cross-process note.** The ``inp`` claim is already atomic across
    processes (the space lock), but the *seed* step is guarded only by the
    per-process ``_ID_LOCK``. A future ``--endpoint`` (HTTP board) must seed
    counters in ``open_space`` before the board is shared and, during
    operation, treat a missing counter as "held" (retry) rather than
    "unseeded" (seed). That is the remaining step to make id autogen fully
    cross-process atomic.
    """
    with _ID_LOCK:
        counter = space.inp(COUNTER_TAG, tag, None, actor=actor)
        if counter is None:
            # No counter: first use (under _ID_LOCK no other in-process writer
            # can be mid-claim, so absence means "unseeded", not "held").
            n = _max_index(tag, space.read_all(tag, None, actor=actor)) + 1
            space.out(COUNTER_TAG, tag, n + 1, actor=actor)
            return f"{prefix}{n}"
        n = counter[2]
        space.out(COUNTER_TAG, tag, n + 1, actor=actor)
        return f"{prefix}{n}"


def make_tuple(tag: str, tid: str, **values) -> tuple:
    """Build a canonical tuple for a curated tag from named field values."""
    s = spec(tag)
    missing = [f for f in s.fields if f not in values]
    if missing:
        raise TypeError(f"{tag} is missing field(s): {', '.join(missing)}")
    return (tag, tid, *(values[f] for f in s.fields))


def search_template(tag: str, **values) -> tuple:
    """Build a read template with ``None`` = wildcard (match anything).

    The user-facing "wildcard search": lock the fields you care about (exact
    match) and leave the rest as ``None``. Unspecified fields become ``None``.
    """
    s = spec(tag)
    return (tag, None, *(values.get(f) for f in s.fields))


def all_template(tag: str) -> tuple:
    """A wildcard template of the correct arity to list every tuple of a tag.

    (A template longer than a tuple never matches, so ``task`` — 3 fields —
    needs ``("task", None, None)``, while knowledge/skill need 5 fields.)
    """
    s = spec(tag)
    return (tag, None, *(None for _ in s.fields))
