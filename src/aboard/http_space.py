"""ABoard HTTP board adapter — join a shared board over the wire.

``--endpoint <url>`` connects a TUI to a ``tuple_space.server`` instance
instead of creating/recovering an in-process ``TupleSpace``. This adapter
presents the same synchronous surface the rest of ABoard uses — ``out``,
``rdp``, ``inp``, ``read_all``, ``count``, ``snapshot``, ``flush_log``,
``close`` — each a ``SyncTupleSpaceClient.dispatch(...)`` that converts JSON
back to tuples. In-process mode (the default) is untouched.

Why a *synchronous* adapter: ABoard's board/config/proposals/workers layers
all call the space synchronously (``space.read_all(...)``, ``space.inp(...)``),
never ``await`` — matching the in-process ``TupleSpace``'s own sync surface.
The stdlib ``SyncTupleSpaceClient`` (no httpx) preserves that shape with one
HTTP round-trip per op.

Caveats, mirroring the plan's "cross-process board" notes:

- **Extended ops aren't on the wire.** The server routes only the Linda
  primitives, so the one extended op ABoard's workers need —
  ``are_dependencies_met`` — is reimplemented here by composing
  ``read_all("dependency", …)`` + ``read_all("completed", …)``.
- **Persistence scoping.** In endpoint mode the server owns the run + WAL.
  This adapter exposes ``run_dir = None``, so client-side resume/archive
  paths no-op gracefully and ``/reset`` is refused by the TUI (see
  ``tui._reset_cmd``).
- **Bounded reads are client-side.** ``read_all(limit=…)``/``reverse=…`` and
  ``count`` are emulated over the wire (the server routes a plain
  ``read_all``); the boundedness is therefore in the slice the client keeps,
  not in the server's read.
- **Id-autogen seed race.** ``reserve_id``'s counter claim (``inp``) is
  atomic across processes, but its *seed* step is not. This adapter seeds
  every counter eagerly on connect, so after a client connects the counters
  exist; ``reserve_id`` then treats a missing counter as "held by a peer"
  (retry) rather than "unseeded". The residual — two clients' *first-ever*
  connection to a brand-new, unseeded server, both inserting as their first
  op — is documented in ``models.reserve_id`` and avoided by starting the
  server with ``--recover`` from a seeded board, or connecting once before
  sharing.
"""

from __future__ import annotations

import json

from tuple_space.sync_client import SyncTupleSpaceClient

from .models import (
    COMPLETED,
    COUNTER_TAG,
    DEPENDENCY,
    TAGS,
    _max_index,
)
from .proposals import PROPOSAL

__all__ = ["HttpSpace"]


def _as_tuple(value):
    """Convert a JSON list (or ``None``) back to a Python tuple."""
    if value is None:
        return None
    return tuple(value)


class HttpSpace:
    """A tuple-space surface backed by an HTTP server (the wire protocol).

    Presents the same synchronous methods ABoard calls on ``TupleSpace``, so
    ``Board``/``config``/``proposals``/``workers``/``chat`` are unchanged.
    Each op is a ``dispatch`` whose JSON response is converted back to the
    tuple shapes the in-process API returns.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = SyncTupleSpaceClient(base_url)
        self._client.__enter__()   # open the persistent keep-alive connection
        # Remote board: the server owns the run + WAL; there is no local run
        # dir to resume/archive. Signals the TUI to refuse client-side reset
        # and skips the archive/resume paths (which key off ``run_dir``).
        self.run_dir = None
        self._remote = True
        self._seed_counters()

    # ── wire dispatch ────────────────────────────────────────────────────

    def _dispatch(self, args: dict) -> dict:
        """One op over the wire, raising on the client's error envelope."""
        raw = self._client.dispatch(args)
        payload = json.loads(raw)
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"remote board error: {payload['error']}")
        return payload

    # ── Linda primitives (the surface ABoard uses) ─────────────────────────

    def out(self, *data, actor=None):
        payload = self._dispatch(
            {"op": "out", "fields": list(data), "actor": actor})
        return payload.get("size")

    def rdp(self, *template, actor=None):
        payload = self._dispatch(
            {"op": "rd", "template": list(template), "actor": actor})
        return _as_tuple(payload.get("tuple"))

    def inp(self, *template, actor=None):
        payload = self._dispatch(
            {"op": "inp", "template": list(template), "actor": actor})
        return _as_tuple(payload.get("tuple"))

    def read_all(self, *template, actor=None, limit=None, reverse=False):
        payload = self._dispatch(
            {"op": "read_all", "template": list(template), "actor": actor})
        tuples = [tuple(t) for t in payload.get("tuples", [])]
        if reverse:
            tuples = list(reversed(tuples))
        if limit is not None:
            tuples = tuples[:limit]
        return tuples

    def count(self, *template, actor=None):
        """Total matching tuples over the wire (the server has no count
        route, so this is ``len(read_all(...))`` — a full remote read)."""
        payload = self._dispatch(
            {"op": "read_all", "template": list(template), "actor": actor})
        return len(payload.get("tuples", []))

    def snapshot(self, actor=None):
        payload = self._dispatch({"op": "snapshot"})
        return [tuple(t) for t in payload.get("tuples", [])]

    def flush_log(self):
        self._dispatch({"op": "flush"})

    def close(self):
        self._client.close()

    # ── extended op reimplemented over the wire ───────────────────────────

    def are_dependencies_met(self, task_id: str) -> bool:
        """Dependency gate, recomposed from Linda primitives (the server does
        not route the substrate's ``are_dependencies_met``).

        Mirrors ``TupleSpace.are_dependencies_met``: satisfied when every
        entry in ``("dependency", task_id, [deps])`` has a matching
        ``("completed", dep, None)`` signal.
        """
        dep = self.rdp(DEPENDENCY, task_id, None)
        if dep is None:
            return True
        deps = list(dep[2])
        completed = {t[1] for t in self.read_all(COMPLETED, None, None)}
        return all(d in completed for d in deps)

    # ── id-autogen bootstrap ──────────────────────────────────────────────

    def _seed_counters(self) -> None:
        """Eagerly seed every id counter once, on connect.

        ABoard autogens ids for the three curated tags and for proposals; a
        ``("id_counter", tag, n)`` tuple per tag makes ``reserve_id``'s claim
        atomic across processes (the ``inp`` take). Seeding here — before the
        board is shared with workers/chat — closes the cross-process seed race
        for the normal case (connect, then act). A counter that already
        exists (a resumed board, or a peer that connected first) is left
        untouched.
        """
        for tag in (*TAGS, PROPOSAL):
            if self.rdp(COUNTER_TAG, tag, None) is not None:
                continue
            n = _max_index(tag, self.read_all(tag, None)) + 1
            self.out(COUNTER_TAG, tag, n)
