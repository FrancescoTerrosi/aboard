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
"""

from __future__ import annotations

import json

from tuple_space import LLMAgent

from .board import board_view
from .models import ARCHITECT, PROJECT

__all__ = ["ArchitectAgent", "ARCHITECT_PROMPT", "ALLOWED_TOOLS"]

# Bounds — keep the conversation chat-sized.
HISTORY_TURNS = 12
TURN_TEXT_MAX = 4000

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
        "Use rdp and read_all to inspect any tuples in the space — task, "
        "knowledge, skill, agent_turn, file_created, and any other tag. "
        "Describe what you find to the human.",
        "Use create_file and scaffold_files to write files. Every path is "
        "relative to your sandbox directory; absolute paths and .. are "
        "rejected.",
        "Never modify the tuple space directly — the only side effects you "
        "create are files.",
        "Always end with a prose reply: say what you found, which files you "
        "created (with their relative paths), or what you suggest next. "
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
                board_view(self.space, actor=self.name)
        return msgs

    # ── the protocol ────────────────────────────────────────────────────

    async def send(self, text: str, *, max_tool_rounds: int = 3) -> dict:
        """One conversational turn. Writes the human turn, then the
        architect's reply when the model produced prose."""
        self.write_turn(self.human, text)
        await self._agent._ensure_registered()
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
                    tool_notes.append(content)
                    continue
                try:
                    content = await self._agent._execute_in_process(
                        tc.function.name, args)
                except Exception as e:  # keep the turn alive on tool failures
                    content = f"error: {e}"
                tool_notes.append(content)
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
        if not reply and tool_notes:
            # A tool-calls-only final message has no prose; without a fallback
            # the transcript would show the human's turn with no architect
            # response. Summarize the tool results so the UI (and a resumed
            # board) still has a complete, readable turn.
            shown = [f"- {_clip(note)}" for note in tool_notes[-6:]]
            reply = "I used my tools:\n" + "\n".join(shown)
        if reply:
            # a tool-calls-only final message is not a real prose turn; only
            # prose (or the fallback summary above) is recorded — the space is
            # the memory
            self.write_turn(self.name, reply)
            self.turns += 1
        # publish the final budget once so /agents-style observability sees it
        self._agent._publish_final_budget()
        return {"reply": reply, "tokens_used": self._agent.tokens_used,
                "api_calls": self._agent.api_calls}
