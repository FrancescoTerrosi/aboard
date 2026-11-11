"""ABoard — Agents Board.

A professional TUI for navigating a tuple space of knowledge, skills, and
tasks shared between humans and agents.

ABoard is a *separate* project built on top of the generic ``tuple_space``
substrate: it defines three conventions (``task``, ``knowledge``, ``skill``)
at the user layer and enforces them via forms + wildcard search — it never
modifies the substrate, which stays schema-flexible underneath.
"""

from .models import (
    AGENT,
    AGENT_TURN,
    ARCHITECT,
    BOOKKEEPING_TAGS,
    COMPLETED,
    COUNTER_TAG,
    DEPENDENCY,
    DERIVED,
    FILE_CREATED,
    FILE_ERROR,
    KNOWLEDGE,
    PROJECT,
    RESET_TAGS,
    REVIEW,
    SKILL,
    TASK,
    TAGS,
    TAG_SPECS,
    TASK_SKILL,
    all_template,
    fields,
    id_prefix,
    key_field,
    make_tuple,
    next_id,
    parse_index,
    reserve_id,
    search_template,
    spec,
)
from . import agents, config
from .architect import ArchitectAgent
from .board import Board, board_view, format_tuples
from .chat import ChatAgent, LLMConfig, MODES
from .discussion import Discussion
from .http_space import HttpSpace
from .llm_agents import GenericAgent
from .proposals import ADD, CLAIM, PROPOSAL, apply, describe, pending, reject, submit
from .reviewer import review
from .session import load_stock, open_space, prior_runs, runs_root

__all__ = [
    "Board",
    "board_view",
    "format_tuples",
    "HttpSpace",
    "GenericAgent",
    "ArchitectAgent",
    "Discussion",
    "review",
    "config",
    "agents",
    "ChatAgent",
    "LLMConfig",
    "MODES",
    "ADD",
    "CLAIM",
    "PROPOSAL",
    "submit",
    "apply",
    "reject",
    "pending",
    "describe",
    "load_stock",
    "open_space",
    "prior_runs",
    "runs_root",
    "TASK",
    "KNOWLEDGE",
    "SKILL",
    "TAGS",
    "TAG_SPECS",
    "spec",
    "fields",
    "key_field",
    "id_prefix",
    "parse_index",
    "next_id",
    "reserve_id",
    "COUNTER_TAG",
    "AGENT",
    "AGENT_TURN",
    "ARCHITECT",
    "REVIEW",
    "DERIVED",
    "PROJECT",
    "FILE_CREATED",
    "FILE_ERROR",
    "TASK_SKILL",
    "DEPENDENCY",
    "COMPLETED",
    "BOOKKEEPING_TAGS",
    "RESET_TAGS",
    "make_tuple",
    "search_template",
    "all_template",
]
