"""ABoard's board layer — enforces the curated templates at the *user* layer,
while leaving the underlying tuple space generic.

Insert = fill a predefined template (validated, id auto-generated).
Search  = exact match with ``None`` wildcards ("wildcard search" in the UI).
Filter  = client-side substring over the loaded board (navigation only; never
          part of the substrate's semantics or the WAL).
"""

from __future__ import annotations

from .models import (
    all_template,
    fields,
    id_prefix,
    key_field,
    make_tuple,
    reserve_id,
    search_template,
    spec,
)


class Board:
    """A thin wrapper over a ``tuple_space.TupleSpace``.

    The substrate stays generic: this class only *adds* the curated
    ``task``/``knowledge``/``skill`` conventions on top of ``out``/
    ``read_all``/``rdp``/``inp``. It does not change what the substrate accepts.

    ``actor`` is the WAL-only attribution for this board's client (the
    ``ops.jsonl`` ``actor`` field — never tuple contents): the TUI passes
    the human's name, a chat agent passes its own; every mutation and read
    is then attributed in the log (the E5 property, carried into ABoard).
    """

    def __init__(self, space, *, actor: str | None = None):
        self.space = space
        self.actor = actor

    # ── insert (enforced template) ──────────────────────────────────────

    def insert(self, tag: str, **values) -> str:
        """Insert one curated tuple, validating the template and auto-generating
        its id. Returns the new id (``t0`` / ``know0`` / ``skill0`` / …).

        Call with the tag's named fields, e.g.::

            board.insert("task", instruction="summarize the README")
            board.insert("knowledge", name="API note", context="dev", text="…")
            board.insert("skill", name="summarize", context="writing", text="…")
        """
        s = spec(tag)  # raises on unknown tag

        unknown = set(values) - set(s.fields)
        if unknown:
            raise TypeError(
                f"unknown field(s) for {tag!r}: {sorted(unknown)} "
                f"(expected {list(s.fields)})"
            )

        full = {f: values.get(f, "") for f in s.fields}
        for f, v in full.items():
            if not isinstance(v, str):
                raise TypeError(f"{f} must be a str, got {type(v).__name__}")

        kf = key_field(tag)
        if not full[kf].strip():
            raise ValueError(f"{kf!r} is required for {tag!r}")

        tid = reserve_id(self.space, tag, id_prefix(tag), actor=self.actor)
        self.space.out(*make_tuple(tag, tid, **full), actor=self.actor)
        return tid

    # ── read / search ───────────────────────────────────────────────────

    def all(self, tag: str) -> list:
        """Every tuple of a curated tag (ordered as stored)."""
        return self.space.read_all(*all_template(tag), actor=self.actor)

    def get(self, tag: str, tid: str):
        """One tuple by exact id, or ``None``."""
        return self.space.rdp(tag, tid, actor=self.actor)

    def search(self, tag: str, **values) -> list:
        """Exact-match retrieval with ``None`` = wildcard ("wildcard search").

        Locks the fields provided and wildcards the rest, mirroring the
        substrate's associative matching. Not substring/contains — that is
        :meth:`filter`.
        """
        return self.space.read_all(*search_template(tag, **values), actor=self.actor)

    def filter(self, tag: str, query: str, *, field: str | None = None) -> list:
        """Client-side substring filter over the loaded board (navigation only).

        ``field`` is one of ``"id"`` or the tag's content fields; ``None``
        searches all fields. Case-insensitive. This never touches the
        substrate's semantics — it's a UX affordance, not a tuple-space query.
        """
        q = query.lower()
        out = []
        for t in self.all(tag):
            if field is None:
                candidates = t
            else:
                candidates = (t[_field_index(tag, field)],)
            if any(q in str(c).lower() for c in candidates):
                out.append(t)
        return out

    def remove(self, tag: str, tid: str):
        """Take a curated tuple by exact id (a destructive, logged mutation)."""
        return self.space.inp(tag, tid, actor=self.actor)

    def import_from(self, stock: list[tuple], tag: str, tid: str) -> str:
        """Copy one curated item from a read-only stock mount into this board.

        The mutation is attributed to *this* Board's actor — the importing
        human, not the stock's author — and goes through :meth:`insert`, so
        the id is re-generated for this board and the template stays
        enforced. The WAL is the provenance record.
        """
        from .models import fields
        for t in stock:
            if t[0] == tag and t[1] == tid:
                values = {f: t[2 + i] for i, f in enumerate(fields(tag))}
                return self.insert(tag, **values)
        raise ValueError(f"no {tag} {tid!r} in the stock mount")


def _field_index(tag: str, field: str) -> int:
    """Map a field name to its tuple position for a tag (``id`` -> 1, else
    2 + position in the tag's content fields)."""
    if field == "id":
        return 1
    fs = fields(tag)
    if field in fs:
        return 2 + fs.index(field)
    raise ValueError(f"unknown field {field!r} for {tag!r}; "
                     f"expected 'id' or one of {list(fs)}")
