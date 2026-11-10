"""Shared deterministic LLM seams for aboard tests (no network).

Builds substrate ``Completion`` dataclasses so a scripted client is
indistinguishable from a real endpoint response for the code under test.
"""

import json

from tuple_space.llm_client import (
    Choice,
    Completion,
    FunctionCall,
    Message,
    ToolCall,
    Usage,
)

__all__ = ["ScriptedClient", "call", "completion"]


def completion(content=None, tool_calls=None, total=10):
    """A scripted LLM response (substrate dataclasses)."""
    msg = Message(content=content, tool_calls=tool_calls)
    return Completion(choices=[Choice(message=msg)],
                      usage=Usage(total_tokens=total))


def call(name, args, i=0):
    """One scripted tool call with JSON-serialized arguments."""
    return ToolCall(id=f"c{i}", function=FunctionCall(name=name,
                                                      arguments=json.dumps(args)))


class ScriptedClient:
    """Queued responses; records every request for assertions.

    A queued ``Exception`` is raised (endpoint unreachable), mirroring how
    the substrate client surfaces failures.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, *, model, messages, tools=None,
                     tool_choice=None, timeout=None):
        """Accepts both call shapes: tool-calling chats pass tools/tool_choice;
        plain completions (workers, planner) omit them."""
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r