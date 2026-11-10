"""ABoard session persistence — resume the board across launches.

The substrate's model: every ``TupleSpace()`` creates a fresh, self-contained
*run* directory with a replayable WAL. Which run is "the board" is not a
substrate decision; it is ABoard's. This module implements it: on launch,
resume the latest prior run with a non-empty final state. Recovery is the
substrate's replay contract (``TupleSpace.recover``) — the resumed board
becomes a new run of its own, so the transcript chain grows one run per
session and every session stays independently replayable.

Semantics (deliberate choices, not accidents):

- **Latest non-empty state wins.** Runs whose final state is empty — a
  launch-and-quit session, or a crash before the WAL flushed — are skipped
  in favor of the newest run that actually holds tuples. A hard crash
  therefore never shadows the real board.
- **Strict on corruption.** A WAL with a mid-file seq gap or an unreadable
  line warns loudly and starts a fresh board. ABoard never prefix-replays
  (that could silently resurrect deleted items) and never deletes prior runs;
  they stay on disk for manual recovery with the substrate's tools.
- **Any run is a candidate.** The board is whatever the latest non-empty run
  held — tuple-space philosophy: one shared board, no private namespaces.
- **``fresh=True`` is per-session.** It starts an empty board and leaves
  prior runs untouched; the next launch resumes as before.

The caller owns the returned space's lifecycle: ``close()`` it when done
(the TUI does so on quit — that fsyncs the WAL, so the next resume sees
everything).
"""

from __future__ import annotations

import os
import re
import shutil
import warnings
from pathlib import Path

from tuple_space import (
    LogCorruptionError,
    ReplayGapError,
    TupleSpace,
    load_run,
    replay_state,
)

from .models import COUNTER_TAG

__all__ = ["archive_prior_runs", "load_stock", "open_space", "prior_runs",
           "reset", "runs_root"]

# Run-dir names: "<utc timestamp>Z-<uuid8>", the substrate's new_run_id()
# scheme (schema-stable, documented in its README). Zero-padded UTC
# timestamps make lexicographic order chronological order.
_RUN_DIR_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def runs_root(run_root: str | None = None) -> Path:
    """Where the substrate creates runs — a mirror of its creation-side
    resolution (``space._resolve_runs_root``), so discovery and creation
    always agree.

    An explicit ``run_root`` wins and is used as-is (runs live directly
    under it). Otherwise ``TUPLE_SPACE_ROOT`` (or the deprecated
    ``TUPLE_SPACE_LOG``) names the *root*, with runs under
    ``<root>/runs/``; the default is ``./.tuplespace/runs/``.
    """
    if run_root is not None:
        return Path(run_root)
    for var in ("TUPLE_SPACE_ROOT", "TUPLE_SPACE_LOG"):
        value = os.environ.get(var)
        if value:
            return Path(value) / "runs"
    return Path(".tuplespace") / "runs"


def prior_runs(root: Path) -> list[Path]:
    """Run directories under ``root``, newest first (missing root → empty).

    Order: the name's UTC timestamp prefix (chronological across seconds),
    ties broken by directory mtime — runs created in the same second (e.g. a
    resume that immediately opens its own run) must not be ordered by their
    random uuid suffix — then by name, so the order is always deterministic.
    """
    if not root.is_dir():
        return []
    entries = []
    for p in root.iterdir():
        if not (p.is_dir() and _RUN_DIR_RE.match(p.name)):
            continue
        ts = p.name[:15]  # "YYYYMMDDTHHMMSS", before the Z
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append(((ts, mtime, p.name), p))
    entries.sort(key=lambda e: e[0], reverse=True)
    return [p for _, p in entries]


def _resume_target(root: Path) -> tuple[Path, int] | None:
    """Newest run holding a non-empty final state: ``(run_dir, item_count)``.

    Walks newest→oldest, skipping runs whose replayed state is empty (valid,
    content-free — safe to skip). Stops at — and raises for — a run that
    cannot be read (corruption or I/O error): broken runs are never silently
    skipped in favor of older boards.
    """
    for run_dir in prior_runs(root):
        # exclude id-counter bookkeeping: a run that only holds counters (an
        # insert-then-delete, or a partially-flushed session) is still empty
        state = [t for t in replay_state(load_run(run_dir))
                 if t[0] != COUNTER_TAG]
        if state:
            return run_dir, len(state)
    return None


def load_stock(root) -> list[tuple]:
    """The latest non-empty board state under a runs root — curated tags only.

    A stock mount is read-only reference material (``--stock`` points at
    another ABoard board's runs root — curate one with ``aboard --run-root``).
    Loading reuses the session rules (latest non-empty run, replay) but is
    deliberately lenient where the project board is strict: an unreadable or
    corrupt stock warns and loads empty rather than blocking the board — the
    stock is shared material, the board is the source of truth.
    """
    from .models import TAGS
    try:
        target = _resume_target(Path(root))
        state = replay_state(load_run(target[0])) if target else []
    except (LogCorruptionError, ReplayGapError, OSError) as e:
        warnings.warn(f"stock {root} is unreadable ({e}); loading empty",
                      UserWarning, stacklevel=2)
        return []
    return [t for t in state if t[0] in TAGS]


def _fresh(run_root: str | None, note: str, warn: bool) -> tuple[TupleSpace, str | None]:
    """Start a fresh board, warning when that is a fallback, not a request."""
    if warn:
        warnings.warn(
            note + " — prior runs are left untouched on disk for manual recovery",
            UserWarning,
            stacklevel=2,
        )
    return TupleSpace(run_root=run_root), note


def _drain(space, tag: str, *, actor: str | None = None) -> int:
    """Take every tuple of one tag (prefix-template claims; attributed)."""
    n = 0
    while space.inp(tag, actor=actor) is not None:
        n += 1
    return n


def archive_prior_runs(space, *, archive_name: str = "archive") -> int:
    """Move every discovered run except ``space``'s own out of discovery's way.

    Why this exists: resume picks the latest run with a *non-empty* state,
    so clearing tuples alone would be undone by the next launch (the emptied
    current run is skipped in favor of older non-empty ones). Archiving
    prior runs makes a reset durable — and reversible: moving a run dir
    back into the root restores it to discovery, byte for byte.
    """
    run_dir = getattr(space, "run_dir", None)
    if run_dir is None:
        return 0  # compat-mode space: no run dir to reason about
    root = run_dir.parent
    current = run_dir.resolve()
    n = 0
    for candidate in prior_runs(root):
        if candidate.resolve() == current:
            continue
        archive = root / archive_name
        archive.mkdir(exist_ok=True)
        dest = archive / candidate.name
        i = 0
        while dest.exists():  # a name from an earlier reset/restore cycle
            i += 1
            dest = archive / f"{candidate.name}.{i}"
        shutil.move(str(candidate), str(dest))
        n += 1
    return n


def reset(space, *, actor: str | None = None) -> dict:
    """Factory reset: clear *everything* — content, settings, transcript.

    Drains every curated tag, chat turns, pending proposals, config tuples,
    and id counters (so a reset board starts ids from scratch, matching the
    pre-counter scan behavior). Every take is a logged, attributed mutation,
    and prior runs are archived (never deleted), so
    the reset is fully recoverable: the WAL records what was cleared,
    ``archive/`` holds the runs — moving one back restores that board,
    settings included.
    """
    from .models import TAGS
    cleared = {tag: _drain(space, tag, actor=actor)
               for tag in (*TAGS, "chat", "proposal", "config", COUNTER_TAG)}
    return {"cleared": cleared, "archived": archive_prior_runs(space)}


def open_space(run_root: str | None = None, *,
               fresh: bool = False) -> tuple[TupleSpace, str | None]:
    """Open the board's space: resume the latest board, or start fresh.

    Returns ``(space, note)`` — ``note`` is a human-readable line for the TUI
    (``None`` when there is nothing worth reporting). ``LogInitError``
    (unwritable runs root) propagates: the substrate's fail-loudly, never
    relocate, contract.
    """
    if fresh:
        n_prior = len(prior_runs(runs_root(run_root)))
        return TupleSpace(run_root=run_root), (
            f"fresh board; {n_prior} prior run(s) left on disk"
            if n_prior else None)

    root = runs_root(run_root)
    try:
        target = _resume_target(root)
        if target is None:
            return TupleSpace(run_root=run_root), None
        run_dir, n = target
        space = TupleSpace.recover(run_dir, run_root=run_root)
    except (LogCorruptionError, ReplayGapError, OSError) as e:
        return _fresh(run_root,
                      f"could not resume the board ({e}); starting fresh",
                      warn=True)
    return space, f"resumed {n} item(s) from run {run_dir.name}"