"""ABoard — Agents Board.

A professional TUI for navigating a tuple space of knowledge, skills, and
tasks shared between humans and agents.

ABoard is a *separate* project built on top of the generic ``tuple_space``
substrate: it defines three conventions (``task``, ``knowledge``, ``skill``)
at the user layer and enforces them via forms + wildcard search — it never
modifies the substrate, which stays schema-flexible underneath.
"""

from .models import (
    COUNTER_TAG,
    KNOWLEDGE,
    SKILL,
    TASK,
    TAGS,
    TAG_SPECS,
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
from . import config
from .board import Board
from .chat import ChatAgent, LLMConfig, MODES
from .proposals import ADD, CLAIM, PROPOSAL, apply, describe, pending, reject, submit
from .session import load_stock, open_space, prior_runs, runs_root

__all__ = [
    "Board",
    "config",
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
    "make_tuple",
    "search_template",
    "all_template",
]
