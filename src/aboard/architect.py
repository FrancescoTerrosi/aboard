"""ABoard architect — a ``/chat``-like, file-capable agent.

Tier 3.4 (refactored): the architect is a *conversational* agent, not an
autonomous one. Like :class:`~aboard.chat.ChatAgent`, the human steers it
turn by turn; unlike the chat agent, the architect can inspect the raw tuple
space (any tag, not just the curated three) and write files and whole
directory trees (``create_file``, ``scaffold_files``) under a per-agent
sandbox directory. Every write is recorded in the space as
``file_created``/``file_error`` tuples, so the board stays the source of
truth for what was scaffolded — the filesystem is the materialization, never
the record.

The transcript is the same shape as chat but namespaced per architect::

    ("architect", name, speaker, text)

so a resumed board resumes every architect conversation too, and
``/architect`` (no arg) re-opens the active thread from the space.

Budget policy (chat parity): the architect is conversational — the human
steers turn by turn — so cost is tracked and reported, never enforced.
Once usage crosses ``token_budget`` every send is still answered (the flag
survives in the space for observability), with ``over_budget`` in the
result so the shell can surface a warning. The substrate's step-boundary
``BudgetExceeded`` stop-before-new-work semantics stay reserved for the
*autonomous* agents (workers, generic agents, discussions), where nobody
approves each turn.
"""

from __future__ import annotations

import json

from tuple_space import LLMAgent

from .board import board_view
from .models import ARCHITECT, FILE_CREATED, PROJECT

__all__ = ["ArchitectAgent", "ARCHITECT_PROMPT", "ALLOWED_TOOLS"]

# Bounds — keep the conversation chat-sized.
HISTORY_TURNS = 12
TURN_TEXT_MAX = 4000
BOARD_PER_TAG = 4
BOARD_FIELD_LIMIT = 200
READ_FIELD_LIMIT = 300
READ_RESULT_MAX = 20

ARCHITECT_PROMPT = (
    "You are an architect agent on a shared board, chatting with a human. "
    "Navigate the tuple space with rdp and read_all — they can read ANY "
    "tuple, not just task/knowledge/skill. Reason with the human about what "
    "is there and produce project files with create_file or scaffold_files. "
    "Every file path is relative to your sandbox directory. Do not modify "
    "the tuple space directly; the only side effects are files you create. "
    "Always end with a prose reply: say what you found and which files you "
    "created (with their relative paths), or what you suggest next. "
    "Be concise."
)


def _architect_system_prompt(name: str, role: str, skills: list[str]) -> str:
    """A chat-facing system prompt (the substrate's base prompt is written
    for autonomous tuple-space coordination, not for a human conversation)."""
    parts = [
        f"You are an architect assistant named {name} (role: {role}), "
        "chatting with a human about a shared tuple space. Help the human "
        "explore the space and produce project files.",
        "DELIVERY RULE: whenever the human asks you to write, create, "
        "produce, plan, scaffold, or save any document, plan, code, or "
        "artifact, ALWAYS produce it with create_file or scaffold_files, "
        "writing the full content out. Never merely describe, outline, or "
        "promise a file — if a file is wanted, the tool call IS the work.",
        "Use rdp and read_all to inspect any tuples in the space — task, "
        "knowledge, skill, agent_turn, file_created, and any other tag. "
        "Describe what you find to the human.",
        "Use create_file and scaffold_files to write files. Every path is "
        "relative to your sandbox directory; absolute paths and .. are "
        "rejected.",
        "Never modify the tuple space directly — the only side effects you "
        "create are files.",
        "Always end with a prose reply: list each file you created with "
        "its relative path, then say what you found or suggest next. "
        "Be concise and conversational.",
    ]
    if skills:
        parts.append(f"Your skills: {', '.join(skills)}.")
    return "\n\n".join(parts)

# The architect is a reader + file-writer, not a board mutator: it navigates
# the space with the human but never writes/claims tuples through the model.
ALLOWED_TOOLS = ("rdp", "read_all", "create_file", "scaffold_files")


def _clip(text: str, limit: int = 200) -> str:
    text = str(text).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


class ArchitectAgent:
    """A conversational, sandboxed file-capable agent.

    ``send(text)`` is the whole protocol: it writes the human's turn, builds
    real message history from the ``("architect", name, speaker, text)``
    tuples, runs the substrate's tool-calling loop (raw reads + file tools),
    writes the architect's reply turn, and returns usage. Inject ``client=``
    with a scripted fake for deterministic tests.

    ``sandbox`` is the directory root for every file the agent writes; the
    substrate's file tools reject absolute paths and ``..`` traversal, so
    writes cannot escape it. On construction the agent declares its project
    as a ``("project", name, sandbox)`` tuple (idempotently), so ``/files``
    and a resumed board can show it.
    """

    def __init__(self, space, *, name: str, role: str, llm,
                 sandbox: str, human: str = "human",
                 skills: list[str] | None = None, client=None):
        if getattr(space, "_remote", False):
            # Files land on this machine; the HTTP wire protocol routes only
            # the Linda primitives, not the substrate's file-tool execution.
            raise ValueError(
                "architect needs an in-process board: files land on this "
                "machine and the wire protocol routes no file ops"
            )
        self.space = space
        self.name = name
        self.role = role
        self.human = human
        self.llm = llm
        self.skills = list(skills or [])
        self.sandbox = sandbox
        self.turns = 0
        self._registered = False

        self._agent = LLMAgent(
            name, role, space=space, model=llm.model,
            base_url=llm.base_url, api_key=llm.api_key,
            token_budget=llm.token_budget, skills=self.skills,
            system_prompt=ARCHITECT_PROMPT,
            enable_file_tool=True, terminal=False, output_dir=sandbox,
        )
        if client is not None:
            self._agent.client = client  # the only transport seam

        # Replace the substrate's coordination prompt with a chat-facing one.
        # The substrate keeps executing the tools (sandboxed file writes,
        # file_created/file_error records, budget/capability registration).
        self._agent.system_prompt = _architect_system_prompt(
            name, role, self.skills)

        # Narrow the substrate's default toolset to "navigate + write files".
        # The substrate still executes the file tools (sandboxed), records
        # file_created/file_error tuples, and tracks budget/capabilities.
        self._agent._tools = [
            t for t in self._agent._tools
            if t["function"]["name"] in ALLOWED_TOOLS
        ]

        # the space records the project so /files can show it (and a resume
        # keeps the declaration with the board). Idempotent: re-opening the
        # same named architect must not duplicate the project tuple.
        if space.rdp(PROJECT, name, None, actor=name) is None:
            space.out(PROJECT, name, sandbox, actor=name)

        # Restore budget state from the space so reopening a named architect
        # does not silently reset its token allowance. The substrate's agent
        # keeps this in memory; we rehydrate usage (never a blocker — the
        # budget reports, it does not refuse, see the budget policy above).
        prior = space.read_all("budget", name, None, None, None, actor=name)
        if prior:
            last = prior[-1]
            self._agent.tokens_used = last[2]

    # ── the space is the memory ────────────────────────────────────────

    @property
    def tokens_used(self) -> int:
        return self._agent.tokens_used

    @property
    def api_calls(self) -> int:
        return self._agent.api_calls

    def history(self) -> list[tuple]:
        """Every turn of this architect thread, as stored."""
        return self.space.read_all(ARCHITECT, self.name, None, None,
                                   actor=self.name)

    def write_turn(self, speaker: str, text: str):
        self.space.out(ARCHITECT, self.name, speaker, text, actor=speaker)

    # ── bounded raw reads ─────────────────────────────────────────────

    @staticmethod
    def _format_tuple(t: tuple, field_limit: int | None = None) -> str:
        def field(v):
            if field_limit is not None and isinstance(v, str) \
                    and len(v) > field_limit:
                return v[:field_limit] + "…"
            return v
        return ", ".join(repr(field(v)) for v in t)

    def _execute_read(self, name: str, args: dict) -> str:
        """Execute rdp/read_all with bounded field sizes.

        Targeted ``rdp`` stays full (the model asked for a specific tuple);
        broad ``read_all`` truncates each field so a full-space scan cannot
        blow the context window.
        """
        template = tuple(self._agent._normalize_template(v)
                         for v in args.get("template", []))
        if name == "rdp":
            result = self.space.rdp(*template, actor=self.name)
            return self._format_tuple(result) if result is not None else "null"
        results = self.space.read_all(*template, actor=self.name)
        if not results:
            return "0 tuples"
        if len(results) <= READ_RESULT_MAX:
            preview = "; ".join(self._format_tuple(t, READ_FIELD_LIMIT)
                                for t in results)
            return f"{len(results)} tuples: {preview}"
        preview = "; ".join(self._format_tuple(t, READ_FIELD_LIMIT)
                            for t in results[:READ_RESULT_MAX])
        return (f"{len(results)} tuples (showing first {READ_RESULT_MAX}): "
                f"{preview}; ...")

    # ── context building ────────────────────────────────────────────────

    def _messages(self) -> list[dict]:
        """System prompt + real conversation history (from the space)."""
        msgs = [{"role": "system", "content": self._agent.system_prompt}]
        for t in self.history()[-HISTORY_TURNS:]:
            speaker, text = t[2], t[3]
            if len(text) > TURN_TEXT_MAX:
                text = text[:TURN_TEXT_MAX] + "…"
            role = "user" if speaker == self.human else "assistant"
            msgs.append({"role": role, "content": text})
        # the last turn is the human's just-written message; append the
        # curated board view so the model always sees the current board
        # state before deciding what to navigate with its raw tuple tools
        if msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n\n[current board]\n" + \
                board_view(self.space, actor=self.name, per_tag=BOARD_PER_TAG,
                           field_limit=BOARD_FIELD_LIMIT)
        return msgs

    # ── the protocol ────────────────────────────────────────────────────

    def _ensure_registered(self) -> None:
        """Publish capabilities and the initial budget once per name.

        The substrate's ``LLMAgent`` would re-publish an initial budget on
        every new instance, so a resumed architect would accumulate duplicate
        budget tuples. We only publish what the space does not already have.
        """
        if self._registered:
            return
        space = self.space
        if self.skills and not space.read_all("capability", self.name, None,
                                              actor=self.name):
            space.out_capability(self.name, self.skills)
        if not space.read_all("budget", self.name, None, None, None,
                              actor=self.name):
            space.out_budget(self.name, 0, self._agent.token_budget,
                             self._agent.token_budget)
        self._registered = True

    async def send(self, text: str, *, max_tool_rounds: int = 3) -> dict:
        """One conversational turn. Writes the human turn, then the
        architect's reply when the model produced prose.

        Budget enforcement is *never blocking* (chat parity): usage is
        tracked across turns and ``over_budget`` is reported in the result
        once ``token_budget`` is crossed, so the shell can warn — the
        conversation itself continues.
        """
        self.write_turn(self.human, text)
        self._ensure_registered()
        # file_created is an append-only event log; the tuples added during
        # this turn are the slice after the pre-turn count (the architect job
        # is exclusive, so nothing else appends concurrently)
        created_before = len(self.space.read_all(
            FILE_CREATED, None, None, None, actor=self.name))
        messages = self._messages()
        tools = self._agent._tools

        response = await self._agent.client.create(
            model=self._agent.model, messages=messages,
            tools=tools, tool_choice="auto", timeout=120,
        )
        self._agent.api_calls += 1
        self._agent._update_token_usage(response.usage)
        message = response.choices[0].message

        tool_notes: list[str] = []
        file_notes: list[str] = []
        for _ in range(max_tool_rounds):
            if not message.tool_calls:
                break
            results = []
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (TypeError, ValueError) as e:
                    # a malformed tool call must not abort the turn: feed the
                    # error back as a tool result so the model can recover
                    content = f"error: malformed arguments: {e}"
                    results.append({"role": "tool", "tool_call_id": tc.id,
                                    "content": content})
                    continue
                try:
                    if tc.function.name in ("rdp", "read_all"):
                        content = self._execute_read(tc.function.name, args)
                    else:
                        content = await self._agent._execute_in_process(
                            tc.function.name, args)
                except Exception as e:  # keep the turn alive on tool failures
                    content = f"error: {e}"
                tool_notes.append(content)
                if tc.function.name in ("create_file", "scaffold_files"):
                    file_notes.append(content)
                results.append({"role": "tool", "tool_call_id": tc.id,
                                "content": content})
            messages.append(message.to_dict())
            messages.extend(results)
            response = await self._agent.client.create(
                model=self._agent.model, messages=messages,
                tools=tools, tool_choice="auto", timeout=120,
            )
            self._agent.api_calls += 1
            self._agent._update_token_usage(response.usage)
            message = response.choices[0].message

        reply = message.content or ""
        if not reply and file_notes:
            # A tool-calls-only final message has no prose; without a fallback
            # the transcript would show the human's turn with no architect
            # response. List the files actually produced rather than dumping
            # every board read.
            shown = [f"- {_clip(note)}" for note in file_notes[-6:]]
            reply = "I created files:\n" + "\n".join(shown)
        elif not reply and tool_notes:
            reply = "I inspected the board with my tools."
        if reply:
            # a tool-calls-only final message is not a real prose turn; only
            # prose (or the fallback summary above) is recorded — the space is
            # the memory
            self.write_turn(self.name, reply)
            self.turns += 1
        # publish the final budget once so /agents-style observability sees it
        self._agent._publish_final_budget()
        created = self.space.read_all(FILE_CREATED, None, None, None,
                                      actor=self.name)[created_before:]
        # one turn may overwrite the same path (create then fix): keep the
        # latest record per path so delivery reporting never double-counts
        latest: dict[str, dict] = {}
        for _, rel, size, _ in created:
            latest[rel] = {"path": rel, "bytes": int(size)}
        return {"reply": reply, "tokens_used": self._agent.tokens_used,
                "api_calls": self._agent.api_calls,
                "files": list(latest.values()),
                "over_budget":
                    self._agent.tokens_used > self._agent.token_budget}
