"""ABoard chat — a conversational agent on the shared board, in two modes.

The wiring, in one place:

- **Transport.** Any OpenAI-compatible endpoint via the substrate's stdlib
  client (no SDK; defaults: a local Ollama). ``--model`` / ``--base-url`` /
  ``--api-key`` override.
- **Two modes** (``--mode``):
  - ``human`` (default): the agent *reads and proposes*. Its write tools are
    :func:`~aboard.proposals.submit` — changes apply only after the human
    approves (``/approve``), so the board stays human-authored. This is E5's
    proposal/decision semantics, carried into ABoard.
  - ``auto`` (opt-in): the agent writes directly via ``board_add`` /
    ``board_claim``. Use it when the board *is* meant to be agent-driven.
- **One write path.** Either way, mutations go through the curated
  :class:`~aboard.board.Board` (validated templates, auto-generated ids) —
  the agent never calls raw ``out`` on the board.
- **Stock mounts** (``--stock``): a read-only snapshot of shared common
  knowledge (another ABoard board's runs root), loaded at launch. The agent
  can cite it (``stock_list`` / ``stock_search``) but never write it; the
  human copies items in with ``/import``.
- **The space is the memory.** Every turn is a ``("chat", speaker, text)``
  tuple; history is rebuilt from the space on each send, so a resumed board
  also resumes the conversation. Pending proposals are mutations too and
  survive restarts.
- **Attribution.** The agent's ops carry ``actor=<agent name>`` in the WAL;
  the human's carry ``actor=<human name>``. Proposals record the full story:
  agent proposed at seq N, human decided at seq M.
- **Cost control.** Crossing ``token_budget`` raises
  :class:`~tuple_space.BudgetExceeded` at the *next* send (the crossing send
  completes — the substrate's step-boundary rule).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from tuple_space import BudgetExceeded
from tuple_space.llm_client import OpenAICompatibleClient

from .board import Board
from .models import TAGS, TAG_SPECS, fields
from .proposals import describe, pending, submit

__all__ = ["ChatAgent", "LLMConfig", "MODES"]

MODES = ("human", "auto")

# bounds — keep the board chat-sized.
HISTORY_TURNS = 12
BOARD_PREVIEW = 8
TOOL_RESULT_MAX = 20
TURN_TEXT_MAX = 4000

FIELDS_HELP = "; ".join(f"{tag}: {', '.join(s.fields)}"
                        for tag, s in TAG_SPECS.items())

CHAT_TAG = "chat"  # ("chat", speaker, text) — the conversation tuple


@dataclass
class LLMConfig:
    """Endpoint settings (defaults match the substrate's pinned values)."""
    model: str = "deepseek-v4.1-flash:cloud"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = "ollama"
    token_budget: int = 100_000


BASE_PROMPT = f"""You are the ABoard assistant, chatting with a human about a shared board.

The board holds three kinds of items (tags), each with fixed fields:
- {FIELDS_HELP}

Use the board_* tools for every read and write; ids are auto-generated.
Prefer searching before adding, to avoid duplicates.
Be concise. Your chat replies are stored on the board verbatim."""

HUMAN_RULES = """Mode: human-driven — you CANNOT change the board directly.
When the human asks you to add or claim something, call board_propose
(kind="add" with the item's fields, or kind="claim" with its id).
The change applies only after the human approves it; tell the human the
proposal id so they can /approve it (or check pending ones yourself with
board_proposals)."""

AUTO_RULES = """Mode: auto — you may change the board directly with board_add
and board_claim. Claim only when the human asks."""

STOCK_RULES = """A read-only stock of shared common knowledge is mounted.
Use stock_list / stock_search to cite it; it can never be written.
The human copies stock items into the board with /import."""


def _system_prompt(mode: str, stock_mounted: bool) -> str:
    parts = [BASE_PROMPT]
    if stock_mounted:
        parts.append(STOCK_RULES)
    parts.append(HUMAN_RULES if mode == "human" else AUTO_RULES)
    return "\n\n".join(parts)


# ── tool schemas ─────────────────────────────────────────────────────────────


def _fn(name: str, description: str, properties: dict,
        required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required}}}


def _read_tool_schema() -> list[dict]:
    """Reads — identical in both modes."""
    return [
        _fn("board_list", "List every item of one tag.",
            {"tag": {"type": "string", "enum": list(TAGS)}}, ["tag"]),
        _fn("board_get", "Get one item by its exact id (e.g. know0).",
            {"tag": {"type": "string", "enum": list(TAGS)},
             "id": {"type": "string"}}, ["tag", "id"]),
        _fn("board_search",
            "Exact-match search: lock the fields you care about, omit the "
            "rest (they become wildcards).",
            {"tag": {"type": "string", "enum": list(TAGS)},
             "values": {"type": "object",
                        "description": "field name → exact value to lock "
                                       "(optional; all wildcards if omitted)"}},
            ["tag"]),
    ]


def _stock_tool_schema() -> list[dict]:
    """Stock reads — read-only, served from the launch-time snapshot."""
    return [
        _fn("stock_list", "List every stock item of one tag.",
            {"tag": {"type": "string", "enum": list(TAGS)}}, ["tag"]),
        _fn("stock_search", "Exact-match search of the stock.",
            {"tag": {"type": "string", "enum": list(TAGS)},
             "values": {"type": "object",
                        "description": "field name → exact value to lock "
                                       "(optional)"}},
            ["tag"]),
    ]


def _human_tool_schema() -> list[dict]:
    """The staging channel: propose, never apply."""
    return [
        _fn("board_propose",
            "Propose one board change (add or claim). Applies only after "
            "the human approves.",
            {"kind": {"type": "string", "enum": ["add", "claim"]},
             "tag": {"type": "string", "enum": list(TAGS)},
             "values": {"type": "object",
                        "description": 'add: the item\'s content fields; '
                                       'claim: {"id": "…"}. '
                                       f"Fields per tag — {FIELDS_HELP}"}},
            ["kind", "tag", "values"]),
        _fn("board_proposals", "List pending proposals awaiting approval.",
            {}, []),
    ]


def _auto_tool_schema() -> list[dict]:
    """Direct writes (opt-in mode)."""
    return [
        _fn("board_add", "Add one item to the board (task, knowledge, or "
                         "skill). The id is auto-generated.",
            {"tag": {"type": "string", "enum": list(TAGS)},
             "values": {"type": "object",
                        "description": f"field name → value ({FIELDS_HELP}). "
                                       "The first field is required and "
                                       "non-empty."}},
            ["tag", "values"]),
        _fn("board_claim", "Claim (REMOVE) one item by its exact id. "
                           "Destructive; only when the human asks.",
            {"tag": {"type": "string", "enum": list(TAGS)},
             "id": {"type": "string"}}, ["tag", "id"]),
    ]


def _tool_schema(mode: str, stock) -> list[dict]:
    tools = _read_tool_schema()
    if stock:
        tools += _stock_tool_schema()
    tools += _human_tool_schema() if mode == "human" else _auto_tool_schema()
    return tools


def _format_tuples(tuples: list, limit: int = TOOL_RESULT_MAX) -> str:
    """Render tuples for the model, repr-quoted like the substrate's agent."""
    if not tuples:
        return "0 items"
    shown = "; ".join(", ".join(repr(v) for v in t) for t in tuples[:limit])
    more = f"; … (+{len(tuples) - limit} more)" if len(tuples) > limit else ""
    return f"{len(tuples)} items: {shown}{more}"


class ChatAgent:
    """A conversational agent whose only write path is the curated Board.

    ``send(text)`` is the whole protocol: it writes the human's turn, builds
    real message history from the chat tuples (plus a bounded board view as
    context — retrieval as context management), runs the tool-calling loop,
    writes the reply turn, and returns usage. Inject ``client=`` with a
    scripted fake for deterministic tests (the client is the only seam).

    Modes: ``human`` (default — read + propose, the human applies) or
    ``auto`` (direct writes). ``stock`` is a read-only launch-time snapshot
    of shared common knowledge (see :func:`aboard.session.load_stock`).
    """

    def __init__(self, space, *, name: str = "assistant", human: str = "human",
                 mode: str = "human", stock: Optional[list[tuple]] = None,
                 llm: LLMConfig | None = None, client=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.space = space
        self.name = name
        self.human = human
        self.mode = mode
        self.stock = stock or None
        self.llm = llm or LLMConfig()
        # the agent's own Board view: every op it does is WAL-attributed to it
        self.board = Board(space, actor=name)
        self.client = client if client is not None else \
            OpenAICompatibleClient(self.llm.base_url, api_key=self.llm.api_key)
        self.tokens_used = 0
        self.api_calls = 0
        self._budget_exceeded = False

    # ── the space is the memory ────────────────────────────────────────

    def history(self) -> list[tuple]:
        """Every chat turn, as stored (read attributed to this agent)."""
        return self.space.read_all(CHAT_TAG, None, None, actor=self.name)

    def write_turn(self, speaker: str, text: str):
        self.space.out(CHAT_TAG, speaker, text, actor=speaker)

    # ── context building ────────────────────────────────────────────────

    def _board_view(self) -> str:
        """A bounded snapshot of the board, injected as turn context."""
        lines = []
        for tag in TAGS:
            tuples = self.board.all(tag)
            lines.append(f"{tag}: " + (_format_tuples(tuples, limit=BOARD_PREVIEW)
                                      if tuples else "none"))
        return "\n".join(lines)

    def _messages(self) -> list[dict]:
        """System prompt + real conversation history (from the space)."""
        msgs = [{"role": "system",
                 "content": _system_prompt(self.mode, self.stock is not None)}]
        for t in self.history()[-HISTORY_TURNS:]:
            speaker, text = t[1], t[2]
            if len(text) > TURN_TEXT_MAX:
                text = text[:TURN_TEXT_MAX] + "…"
            role = "user" if speaker == self.human else "assistant"
            msgs.append({"role": role, "content": text})
        # the last turn is the human's just-written message; append the board
        # view to it so the model always sees the current board state
        if msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n\n[current board]\n" + self._board_view()
        return msgs

    # ── tool execution (curated Board only) ─────────────────────────────

    def _stock_find(self, tag: str, values: Optional[dict] = None) -> list[tuple]:
        """In-memory wildcard search over the read-only stock snapshot."""
        out = []
        fs = fields(tag)
        for t in self.stock or []:
            if t[0] != tag:
                continue
            if all(t[2 + fs.index(f)] == v for f, v in (values or {}).items()):
                out.append(t)
        return out

    async def _execute(self, name: str, args: dict) -> str:
        try:
            if name in ("board_add", "board_claim") and self.mode == "human":
                return ("error: direct writes are disabled in human-driven "
                        "mode; use board_propose")
            if name == "board_add":
                tid = self.board.insert(args["tag"], **args.get("values", {}))
                return f"ok: {args['tag']} {tid}"
            if name == "board_list":
                return _format_tuples(self.board.all(args["tag"]))
            if name == "board_get":
                t = self.board.get(args["tag"], args.get("id", ""))
                return ", ".join(repr(v) for v in t) if t else "null"
            if name == "board_search":
                return _format_tuples(
                    self.board.search(args["tag"], **args.get("values", {})))
            if name == "board_claim":
                t = self.board.remove(args["tag"], args.get("id", ""))
                return (", ".join(repr(v) for v in t)) if t else "error: not found"
            if name == "board_propose":
                pid = submit(self.space, args["kind"], args["tag"],
                             args.get("values", {}), actor=self.name)
                return f"proposed: {pid} — pending the human's approval (/approve {pid})"
            if name == "board_proposals":
                ps = pending(self.space)
                if not ps:
                    return "no pending proposals"
                return "; ".join(describe(t) for t in ps[:TOOL_RESULT_MAX])
            if name == "stock_list":
                return _format_tuples(self._stock_find(args["tag"]))
            if name == "stock_search":
                return _format_tuples(
                    self._stock_find(args["tag"], args.get("values", {})))
        except (KeyError, TypeError, ValueError) as e:
            return f"error: {e}"
        return f"error: unknown tool {name!r}"

    # ── the protocol ────────────────────────────────────────────────────

    def _update_tokens(self, usage) -> None:
        """Accumulate usage; cross → raise at the NEXT send (the crossing
        send completes, mirroring the substrate's step-boundary rule)."""
        self.tokens_used += getattr(usage, "total_tokens", 0)
        if self.tokens_used > self.llm.token_budget:
            self._budget_exceeded = True

    async def send(self, text: str, *, max_tool_rounds: int = 3) -> dict:
        """One conversational turn. Writes both tuples (human, then reply)."""
        if self._budget_exceeded:
            raise BudgetExceeded(
                f"chat agent {self.name} exceeded token budget: "
                f"{self.tokens_used:,} / {self.llm.token_budget:,}"
            )
        self.write_turn(self.human, text)
        messages = self._messages()
        tools = _tool_schema(self.mode, self.stock)

        response = await self.client.create(
            model=self.llm.model, messages=messages,
            tools=tools, tool_choice="auto", timeout=120,
        )
        self.api_calls += 1
        self._update_tokens(response.usage)
        message = response.choices[0].message

        for _ in range(max_tool_rounds):
            if not message.tool_calls:
                break
            results = []
            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments)
                results.append({"role": "tool", "tool_call_id": tc.id,
                                "content": await self._execute(tc.function.name, args)})
            messages.append(message.to_dict())
            messages.extend(results)
            response = await self.client.create(
                model=self.llm.model, messages=messages,
                tools=tools, tool_choice="auto", timeout=120,
            )
            self.api_calls += 1
            self._update_tokens(response.usage)
            message = response.choices[0].message

        reply = message.content or "(tool calls only)"
        self.write_turn(self.name, reply)
        return {"reply": reply, "tokens_used": self.tokens_used,
                "api_calls": self.api_calls}