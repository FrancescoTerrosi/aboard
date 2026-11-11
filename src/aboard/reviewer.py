"""ABoard reviewer — a multi-agent pattern that reviews pending proposals.

Tier 3.2 (reviewer): a reviewer agent reads the proposal inbox and writes a
*verdict* tuple for each pending proposal — ``("review", pid, verdict,
rationale)`` where ``verdict`` is ``"approve"`` / ``"reject"`` / ``"abstain"``.
This is the "reviewer" coordination pattern carried into ABoard's
human-authored safety model: the reviewer *recommends*, it never applies —
``/approve`` / ``/reject`` remain the human's decision, so the board stays
human-authored (the plan's safety property). An agent-driven "auto-approve"
is deliberately out of scope here: side effects before human approval break
that property.

The reviewer reads only (``pending``) and writes only verdicts — both through
the tuple space, so the pattern is exactly "coordinate through the board".
"""

from __future__ import annotations

from typing import Optional

from tuple_space.llm_client import OpenAICompatibleClient

from .board import board_view
from .models import REVIEW
from .proposals import describe, pending

__all__ = ["VERDICTS", "review"]

VERDICTS = ("approve", "reject", "abstain")

REVIEWER_PROMPT = (
    "You are a reviewer on a shared board. Given a proposed board change, "
    "decide whether it should be approved. Reply with exactly one line: "
    "APPROVE <reason>, REJECT <reason>, or ABSTAIN <reason>. Be terse."
)


def _parse_verdict(raw: str) -> tuple[str, str]:
    """Parse a reviewer reply into ``(verdict, rationale)``.

    The first word is the verdict; anything unparseable is a safe ``abstain``
    (a confused reviewer must never be read as an approval)."""
    raw = (raw or "").strip()
    word, _, rationale = raw.partition(" ")
    verdict = word.lower().rstrip(".:")
    if verdict not in VERDICTS:
        return "abstain", (raw or "(no rationale)")
    return verdict, rationale.strip()


def _review_context(space, target, actor=None) -> str:
    """The reviewer's brief: the board + sibling proposals + the target.

    The bounded board view grounds the reviewer in current knowledge/tasks;
    listing the *other* pending proposals lets it catch cross-proposal
    inconsistencies (e.g. one artifact assumes a different port than the
    rest) — the reconciliation a human would otherwise do by eye.
    """
    parts = ["[current board]\n" + board_view(space, actor=actor)]
    siblings = [describe(p) for p in pending(space, actor=actor)
                if p[1] != target[1]]
    if siblings:
        parts.append("Other pending proposals:\n" +
                     "\n".join(f"- {s}" for s in siblings))
    parts.append("Review this proposal:\n" + describe(target))
    return "\n\n".join(parts)


async def review(space, *, llm, pid: Optional[str] = None,
                 actor: Optional[str] = None, client=None) -> list[str]:
    """Review pending proposals (or one ``pid``) and write verdict tuples.

    Returns one human-readable line per verdict. Inject ``client=`` with a
    scripted fake for deterministic tests. A failed call stops the review
    (raises) rather than writing a guess — no verdict is better than a wrong
    one.
    """
    client = client if client is not None else \
        OpenAICompatibleClient(llm.base_url, api_key=llm.api_key)

    targets = pending(space, actor=actor)
    if pid is not None:
        targets = [p for p in targets if p[1] == pid]
    if not targets:
        return [f"no {'proposal ' + pid if pid else 'pending proposals'}"]

    out = []
    for p in targets:
        response = await client.create(
            model=llm.model,
            messages=[{"role": "system", "content": REVIEWER_PROMPT},
                      {"role": "user", "content": _review_context(space, p, actor)}],
            timeout=120)
        verdict, rationale = _parse_verdict(
            response.choices[0].message.content)
        space.out(REVIEW, p[1], verdict, rationale, actor=actor)
        out.append(f"{p[1]}: {verdict} — {rationale}")
    return out
