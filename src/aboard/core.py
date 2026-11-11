"""ABoard core — the UI-free backend behind the TUI and the web shell.

``BoardService`` owns the same state and command semantics that used to live
inside ``tui.BoardApp``. Shells (Textual, browser) are thin adapters: they
hold view-only state (active tag, filter query, cursor) and translate the
service's structured results/events into widgets or JSON.

The service has no Textual and no HTTP imports. Long-running work is returned
as :class:`Job` objects whose ``start(observer)`` builds a coroutine; each
shell schedules those coroutines in its own way (``run_worker`` in Textual,
``asyncio.create_task`` in the web shell).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Coroutine, Optional, Protocol

from tuple_space import TupleSpace

from . import agents, config
from .architect import ArchitectAgent
from .board import Board, format_tuples
from .chat import ChatAgent, LLMConfig, MODES
from .discussion import Discussion
from .llm_agents import GenericAgent
from .models import (AGENT_TURN, ARCHITECT, DEPENDENCY, FILE_CREATED,
                     FILE_ERROR, PROJECT, RESET_TAGS, REVIEW, TAGS,
                     TASK_SKILL, fields)
from .proposals import apply, describe, pending, reject
from .reviewer import review
from .session import load_stock, prior_runs, reset
from .workers import TaskWorker, plan

__all__ = [
    "BoardService",
    "CommandContext",
    "CommandResult",
    "Job",
    "Notice",
    "Observer",
    "TableState",
    "ViewState",
]

INSPECT_PER_TAG = 50


@dataclass
class Notice:
    message: str
    level: str = "info"  # "info" | "warning" | "error"


@dataclass
class ViewState:
    name: str
    data: object = None
    scroll_end: bool = False


@dataclass
class TableState:
    tag: str
    rows: list[tuple]
    subtitle: str


@dataclass
class Job:
    """One unit of background work for a shell to schedule.

    ``start(observer)`` returns the coroutine to run. It is a callable rather
    than a coroutine because the shell owns the observer instance (Textual
    wraps its widgets, the web shell wraps an SSE stream).
    """
    start: Callable[["Observer"], Coroutine]
    group: str
    exclusive: bool = False


@dataclass
class CommandResult:
    messages: list[Notice] = field(default_factory=list)
    view: ViewState | None = None
    table: TableState | None = None
    jobs: list[Job] = field(default_factory=list)


@dataclass
class CommandContext:
    """View-only state a command needs from the shell."""
    tag: str = TAGS[0]
    tid: str | None = None
    proposal_pid: str | None = None


class Observer(Protocol):
    """Sink for progress emitted by long-running service operations."""

    def on_message(self, message: str, *, level: str = "info") -> None: ...

    def on_view(self, state: ViewState) -> None: ...

    def on_table(self, state: TableState) -> None: ...

    def on_state(self) -> None: ...


class BoardService:
    """UI-free owner of the board, live agents, and command semantics."""

    def __init__(self, space: TupleSpace | None = None,
                 board: Board | None = None,
                 *, human: str = "human",
                 chat_mode: str = "human",
                 stock: list[tuple] | None = None,
                 llm: LLMConfig | None = None,
                 chat_agent: ChatAgent | None = None,
                 architect_agent: ArchitectAgent | None = None):
        self.board = board if board is not None else Board(
            space if space is not None else TupleSpace())
        self.human = human
        self.chat_mode = chat_mode
        self.stock = stock
        self.llm = llm
        self._chat_agent = chat_agent
        self._architect_agent = architect_agent
        self._architect_name = architect_agent.name if architect_agent else None
        self._task_workers: dict[str, TaskWorker] = {}
        self._llm_agents: dict[str, GenericAgent] = {}
        self._discussions: list[Discussion] = []
        self._worker_seq = 0
        self._reset_armed = False

    # ── shell convenience ──────────────────────────────────────────────

    @property
    def space(self):
        return self.board.space

    @property
    def task_workers(self):
        return self._task_workers

    @property
    def llm_agents(self):
        return self._llm_agents

    @property
    def discussions(self):
        return self._discussions

    @property
    def reset_armed(self):
        return self._reset_armed

    @property
    def architect_agent(self):
        return self._architect_agent

    @property
    def architect_name(self):
        return self._architect_name

    def ensure_chat_agent(self) -> ChatAgent:
        if self._chat_agent is None:
            self._chat_agent = ChatAgent(
                self.space, human=self.human, mode=self.chat_mode,
                stock=self.stock,
                llm=self.llm if self.llm is not None else LLMConfig())
        return self._chat_agent

    # ── structured reads ────────────────────────────────────────────────

    def list_items(self, tag: str, query: str = "") -> list[tuple]:
        return (self.board.filter(tag, query) if query
                else self.board.all(tag))

    def table_state(self, tag: str, query: str = "") -> TableState:
        rows = self.list_items(tag, query)
        return TableState(tag=tag, rows=rows,
                          subtitle=f"{len(rows)} {tag} · "
                                   f"{self.proposal_count()} proposal(s) pending")

    def item_detail(self, tag: str, tid: str):
        return self.board.get(tag, tid)

    def proposal_count(self) -> int:
        return len(pending(self.space, actor=self.human))

    def proposals(self) -> list[tuple]:
        return pending(self.space, actor=self.human)

    def proposals_snapshot(self) -> dict:
        ps = self.proposals()
        reviews = {p[1]: self.reviews_for(p[1]) for p in ps}
        return {"proposals": ps, "reviews": reviews}

    def reviews_for(self, pid: str) -> list[tuple]:
        return self.space.read_all(REVIEW, pid, None, None, actor=self.human)

    def chat_transcript(self) -> list[tuple]:
        return self.space.read_all("chat", None, None, actor=self.human)

    def architect_transcript(self, name: str | None = None) -> list[tuple]:
        name = name or self._architect_name
        if not name:
            return []
        return self.space.read_all(ARCHITECT, name, None, None,
                                   actor=self.human)

    def agent_defs(self) -> list[tuple]:
        return agents.all_agents(self.space, actor=self.human)

    def status_snapshot(self) -> dict:
        space = self.space
        workers = [
            {"name": w.name, "tasks_done": w.tasks_done,
             "tokens_used": w.tokens_used}
            for w in self._task_workers.values()
        ]
        generic = [
            {"name": a.name, "role": a.role, "turns": a.turns,
             "tokens_used": a.tokens_used}
            for a in self._llm_agents.values()
        ]
        architect = None
        if self._architect_agent is not None:
            a = self._architect_agent
            architect = {"name": a.name, "role": a.role,
                         "skills": list(a.skills), "turns": a.turns,
                         "tokens_used": a.tokens_used}
        return {
            "tasks": len(space.read_all("task", None, None, actor=self.human)),
            "proposals": self.proposal_count(),
            "agent_defs": self.agent_defs(),
            "workers": workers,
            "generic_agents": generic,
            "architect": architect,
            "chat_mode": self.chat_mode,
        }

    def tuples_snapshot(self, tag: str | None) -> dict:
        space = self.space
        if tag:
            return {"tag": tag,
                    "tuples": space.read_all(tag, None, actor=self.human)}
        snap = space.snapshot(actor=self.human)
        by_tag: dict[str, list] = {}
        for t in snap:
            by_tag.setdefault(t[0], []).append(t)
        return {"tag": None, "groups": by_tag, "total": len(snap)}

    def files_snapshot(self) -> dict:
        space = self.space
        projects = space.read_all(PROJECT, None, None, actor=self.human)
        created = space.read_all(FILE_CREATED, None, None, None,
                                 actor=self.human)
        errors = space.read_all(FILE_ERROR, None, None, None,
                                actor=self.human)
        # file_created is an append-only event log; for the manifest view we
        # collapse to the latest record per relative path (an overwrite must
        # not look like two separate files).
        latest_files: dict[str, tuple] = {}
        for f in created:
            latest_files[f[1]] = f
        latest_errors: dict[str, tuple] = {}
        for e in errors:
            latest_errors[e[1]] = e
        return {
            "projects": projects,
            "files": sorted(latest_files.values(), key=lambda t: t[1]),
            "errors": sorted(latest_errors.values(), key=lambda t: t[1]),
        }

    def help_snapshot(self) -> dict:
        return {
            "chat_mode": self.chat_mode,
            "stock": bool(self.stock),
            "proposals": self.proposal_count(),
        }

    # ── command dispatch ────────────────────────────────────────────────

    def handle(self, line: str, *, ctx: CommandContext | None = None) -> CommandResult:
        line = line.strip()
        if not line:
            return CommandResult()
        ctx = ctx or CommandContext()
        if not line.startswith("/"):
            return self._cmd_chat(line, ctx)
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if self._reset_armed and cmd != "reset":
            self._reset_armed = False
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return CommandResult(messages=[
                Notice(f"unknown command /{cmd} — /help lists all", "error")
            ])
        return handler(arg, ctx)

    # ── synchronous commands ────────────────────────────────────────────

    def _table(self, tag: str, query: str = "") -> TableState:
        return self.table_state(tag, query)

    def _cmd_add(self, arg: str, ctx: CommandContext) -> CommandResult:
        tag, _, rest = arg.partition(" ")
        if tag not in TAGS:
            return CommandResult(messages=[
                Notice(f"usage: /add <{'|'.join(TAGS)}> …", "error")
            ])
        fs = fields(tag)
        parts = [p.strip() for p in rest.split("|")]
        parts += [""] * (len(fs) - len(parts))
        values = dict(zip(fs, parts[:len(fs)]))
        try:
            tid = self.board.insert(tag, **values)
        except (TypeError, ValueError) as e:
            return CommandResult(messages=[Notice(str(e), "error")])
        return CommandResult(
            messages=[Notice(f"added {tag} {tid}")],
            table=self._table(tag),
        )

    def _cmd_del(self, arg: str, ctx: CommandContext) -> CommandResult:
        if not ctx.tid:
            return CommandResult(messages=[
                Notice("nothing selected", "warning")
            ])
        self.board.remove(ctx.tag, ctx.tid)
        return CommandResult(
            messages=[Notice(f"removed {ctx.tag} {ctx.tid}")],
            table=self._table(ctx.tag),
        )

    def _cmd_search(self, arg: str, ctx: CommandContext) -> CommandResult:
        tag, _, rest = arg.partition(" ")
        if tag not in TAGS:
            return CommandResult(messages=[
                Notice(f"usage: /search <{'|'.join(TAGS)}> <text>", "error")
            ])
        key = fields(tag)[0]
        rows = self.board.search(tag, **{key: rest})
        return CommandResult(view=ViewState(
            "search", {"tag": tag, "query": rest, "rows": rows}))

    def _cmd_clear(self, arg: str, ctx: CommandContext) -> CommandResult:
        tag = arg.strip() or ctx.tag
        if tag not in TAGS:
            return CommandResult(messages=[Notice(f"unknown tag {tag!r}", "error")])
        for t in self.board.all(tag):
            self.board.remove(tag, t[1])
        return CommandResult(
            messages=[Notice(f"cleared {tag}")],
            table=self._table(tag),
        )

    def _cmd_proposals(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState(
            "proposals", self.proposals_snapshot()))

    def _cmd_approve(self, arg: str, ctx: CommandContext) -> CommandResult:
        return self._decide(arg.strip() or ctx.proposal_pid, approve=True,
                            tag=ctx.tag)

    def _cmd_reject(self, arg: str, ctx: CommandContext) -> CommandResult:
        return self._decide(arg.strip() or ctx.proposal_pid, approve=False,
                            tag=ctx.tag)

    def _decide(self, pid: str | None, *, approve: bool,
                tag: str = TAGS[0]) -> CommandResult:
        if approve:
            msg = apply(self.space, self.board, pid, actor=self.human)
        else:
            msg = reject(self.space, pid, actor=self.human)
        return CommandResult(
            messages=[Notice(msg)],
            table=self.table_state(tag),
        )

    def _cmd_import(self, arg: str, ctx: CommandContext) -> CommandResult:
        if not self.stock:
            return CommandResult(messages=[
                Notice("no stock mounted — /stock <runs root> to mount", "warning")
            ])
        tag, _, tid = arg.strip().partition(" ")
        if tag not in TAGS or not tid:
            return CommandResult(messages=[
                Notice(f"usage: /import <{'|'.join(TAGS)}> <id>", "error")
            ])
        try:
            new_id = self.board.import_from(self.stock, tag, tid)
        except ValueError as e:
            return CommandResult(messages=[Notice(str(e), "error")])
        return CommandResult(
            messages=[Notice(f"imported {tag} {tid} → {new_id}")],
            table=self._table(tag),
        )

    def _cmd_mode(self, arg: str, ctx: CommandContext) -> CommandResult:
        arg = arg.strip()
        if not arg:
            return CommandResult(messages=[Notice(
                f"chat mode: {self.chat_mode} — switch with "
                f"{' or '.join(f'/mode {m}' for m in MODES)}")])
        if arg not in MODES:
            return CommandResult(messages=[
                Notice(f"mode must be one of {MODES}", "error")
            ])
        self.chat_mode = arg
        config.store(self.space, config.MODE, arg, actor=self.human)
        if self._chat_agent is not None:
            self._chat_agent.mode = arg
        return CommandResult(messages=[Notice(
            "chat mode: auto — the agent now writes directly"
            if arg == "auto" else
            "chat mode: human — the agent proposes; /approve applies")])

    def _cmd_stock(self, arg: str, ctx: CommandContext) -> CommandResult:
        arg = arg.strip()
        if not arg:
            return CommandResult(view=ViewState(
                "stock", self.stock_snapshot()))
        if arg == "off":
            config.clear(self.space, config.STOCK, actor=self.human)
            self.stock = None
            if self._chat_agent is not None:
                self._chat_agent.stock = None
            return CommandResult(messages=[Notice("stock unmounted")])
        if not Path(arg).is_dir():
            return CommandResult(messages=[
                Notice(f"no such directory: {arg}", "error")
            ])
        items = load_stock(arg)
        self.stock = items or None
        config.store(self.space, config.STOCK, arg, actor=self.human)
        if self._chat_agent is not None:
            self._chat_agent.stock = items or None
        return CommandResult(messages=[Notice(
            f"stock mounted: {len(items)} item(s) — /import <tag> <id> to copy")])

    def stock_snapshot(self) -> dict:
        path = config.get(self.space, config.STOCK, actor=self.human)
        items = self.stock or []
        counts = {tag: sum(1 for t in items if t[0] == tag) for tag in TAGS}
        return {"mounted": bool(self.stock), "path": path,
                "count": len(items), "counts": counts}

    def _cmd_tag(self, arg: str, ctx: CommandContext) -> CommandResult:
        tid, _, skill = arg.partition(" ")
        tid, skill = tid.strip(), skill.strip()
        if not tid or not skill:
            return CommandResult(messages=[
                Notice("usage: /tag <task_id> <skill>", "error")
            ])
        if self.space.rdp("task", tid, None) is None:
            return CommandResult(messages=[
                Notice(f"no task {tid!r} on the board", "warning")
            ])
        self.space.out(TASK_SKILL, tid, skill, actor=self.human)
        return CommandResult(messages=[
            Notice(f"task {tid} requires skill {skill!r}")
        ])

    def _cmd_dep(self, arg: str, ctx: CommandContext) -> CommandResult:
        parts = arg.split()
        if len(parts) < 2:
            return CommandResult(messages=[
                Notice("usage: /dep <task_id> <dep1> [dep2…]", "error")
            ])
        tid, deps = parts[0], parts[1:]
        if self.space.rdp("task", tid, None) is None:
            return CommandResult(messages=[
                Notice(f"no task {tid!r} on the board", "warning")
            ])
        self.space.inp(DEPENDENCY, tid, None, actor=self.human)
        self.space.out(DEPENDENCY, tid, deps, actor=self.human)
        return CommandResult(messages=[
            Notice(f"task {tid} depends on {', '.join(deps)}")
        ])

    def _cmd_status(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState("status", self.status_snapshot()))

    def _cmd_help(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState("help", self.help_snapshot()))

    def _cmd_agent(self, arg: str, ctx: CommandContext) -> CommandResult:
        sub, _, rest = arg.partition(" ")
        sub = sub.strip()
        if sub in ("", "list"):
            return CommandResult(view=ViewState(
                "agent_defs", self.agent_defs_snapshot()))
        if sub == "add":
            return self._agent_add(rest.strip())
        if sub == "start":
            return self._agent_start(rest.strip())
        return CommandResult(messages=[Notice(
            "usage: /agent [list] · /agent add <name> <role> [skills…] · "
            "/agent start <name>", "error")])

    def agent_defs_snapshot(self) -> dict:
        return {"defs": self.agent_defs(),
                "running": set(self._task_workers)}

    def _agent_add(self, rest: str) -> CommandResult:
        parts = rest.split()
        if len(parts) < 2:
            return CommandResult(messages=[
                Notice("usage: /agent add <name> <role> [skills…]", "error")
            ])
        name, role = parts[0], parts[1]
        skills = parts[2:]
        try:
            agents.add(self.space, name, role, skills, actor=self.human)
        except ValueError as e:
            return CommandResult(messages=[Notice(str(e), "error")])
        skills_s = f" · skills: {', '.join(skills)}" if skills else ""
        return CommandResult(messages=[Notice(f"agent {name} added: {role}{skills_s}")])

    def _agent_start(self, name: str) -> CommandResult:
        if not name:
            return CommandResult(messages=[
                Notice("usage: /agent start <name>", "error")
            ])
        defn = agents.get(self.space, name, actor=self.human)
        if defn is None:
            return CommandResult(messages=[Notice(
                f"no agent {name!r} — /agent add {name} <role> [skills…]",
                "error")])
        if name in self._task_workers:
            return CommandResult(messages=[
                Notice(f"agent {name} is already running", "warning")
            ])
        _, _, role, skills = defn
        w = TaskWorker(self.space, name=name, llm=self.llm or LLMConfig(),
                       role=role, skills=skills)
        self._task_workers[name] = w
        return CommandResult(
            messages=[Notice(f"agent {name} started ({role})")],
            jobs=[Job(start=lambda obs: self._run_task_worker(name, w, obs),
                      group=f"worker-{name}")],
        )

    # ── async / job-producing commands ──────────────────────────────────

    def _cmd_chat(self, arg: str, ctx: CommandContext) -> CommandResult:
        if not arg.strip():
            if not self.chat_transcript():
                return CommandResult(messages=[
                    Notice("no chat yet — /chat <text> to start", "warning")
                ])
            return CommandResult(view=ViewState(
                "chat", {"turns": self.chat_transcript()}))
        return CommandResult(jobs=[
            Job(start=lambda obs: self.do_chat(arg, obs),
                group="chat", exclusive=True)
        ])

    def _cmd_plan(self, arg: str, ctx: CommandContext) -> CommandResult:
        tid = arg.strip() or (ctx.tid if ctx.tag == TAGS[0] else None)
        if not tid:
            return CommandResult(messages=[
                Notice("highlight a task row, or: /plan <task id>", "warning")
            ])
        return CommandResult(jobs=[
            Job(start=lambda obs: self.do_plan(tid, obs),
                group="plan", exclusive=True)
        ])

    def _cmd_work(self, arg: str, ctx: CommandContext) -> CommandResult:
        try:
            n = max(1, int(arg.strip() or 1))
        except ValueError:
            return CommandResult(messages=[Notice("usage: /work [n]", "error")])
        started = []
        jobs = []
        for _ in range(n):
            name = f"w{self._worker_seq}"
            self._worker_seq += 1
            w = TaskWorker(self.space, name=name, llm=self.llm or LLMConfig())
            self._task_workers[name] = w
            jobs.append(Job(start=lambda obs, name=name, w=w: self._run_task_worker(name, w, obs),
                            group=f"worker-{name}"))
            started.append(name)
        return CommandResult(
            messages=[Notice(
                f"workers started: {', '.join(started)} — "
                "claim → execute → propose; /approve applies, /stop halts")],
            jobs=jobs,
        )

    def _cmd_spawn(self, arg: str, ctx: CommandContext) -> CommandResult:
        parts = arg.split()
        if len(parts) < 2:
            return CommandResult(messages=[
                Notice("usage: /spawn <name> <role> [skills…]", "error")
            ])
        name, role = parts[0], parts[1]
        skills = parts[2:]
        if name in self._task_workers or name in self._llm_agents:
            return CommandResult(messages=[
                Notice(f"{name!r} is already running", "warning")
            ])
        try:
            a = GenericAgent(self.space, name=name, role=role,
                             llm=self.llm or LLMConfig(), skills=skills)
        except ValueError as e:
            return CommandResult(messages=[Notice(str(e), "error")])
        self._llm_agents[name] = a
        skills_s = f" · skills: {', '.join(skills)}" if skills else ""
        return CommandResult(
            messages=[Notice(f"agent {name} spawned: {role}{skills_s} — "
                             "/agents to watch")],
            jobs=[Job(start=lambda obs: self._run_generic_agent(name, a, obs),
                      group=f"agent-{name}")],
        )

    def _cmd_architect(self, arg: str, ctx: CommandContext) -> CommandResult:
        arg = arg.strip()
        if not arg:
            return CommandResult(view=ViewState(
                "architect", self.architect_snapshot()))
        sub, _, rest = arg.partition(" ")
        if sub == "start":
            return self._architect_start(rest.strip())
        if self._architect_agent is None:
            return CommandResult(messages=[Notice(
                "no active architect — /architect start <name> <role> [skills…]",
                "warning")])
        return CommandResult(jobs=[
            Job(start=lambda obs: self.do_architect(arg, obs),
                group="architect", exclusive=True)
        ])

    def _architect_sandbox(self, name: str) -> str:
        run_dir = self.space.run_dir
        base = Path(run_dir).parent if run_dir else Path.home() / ".aboard"
        return str(base / "projects" / name)

    def architect_snapshot(self) -> dict:
        name = self._architect_name
        return {"name": name, "turns": self.architect_transcript(name)}

    def _architect_start(self, rest: str) -> CommandResult:
        parts = rest.split()
        if len(parts) < 2:
            return CommandResult(messages=[
                Notice("usage: /architect start <name> <role> [skills…]", "error")
            ])
        name, role = parts[0], parts[1]
        skills = parts[2:]
        if name in self._task_workers or name in self._llm_agents:
            return CommandResult(messages=[
                Notice(f"{name!r} is already running", "warning")
            ])
        if getattr(self.space, "_remote", False):
            return CommandResult(messages=[Notice(
                "architect needs an in-process board (files land on this machine)",
                "warning")])
        sandbox = self._architect_sandbox(name)
        try:
            a = ArchitectAgent(self.space, name=name, role=role,
                               human=self.human, llm=self.llm or LLMConfig(),
                               sandbox=sandbox, skills=skills)
        except ValueError as e:
            return CommandResult(messages=[Notice(str(e), "error")])
        self._architect_agent = a
        self._architect_name = name
        skills_s = f" · skills: {', '.join(skills)}" if skills else ""
        return CommandResult(
            messages=[Notice(f"architect {name} started ({role}){skills_s} — "
                             f"sandbox {sandbox} · /architect <text> to talk")],
            view=ViewState("architect", self.architect_snapshot(),
                           scroll_end=True),
        )

    def _cmd_agents(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState(
            "llm_agents", self.llm_agents_snapshot()))

    def llm_agents_snapshot(self) -> dict:
        out = []
        for name, a in self._llm_agents.items():
            turns = self.space.read_all(AGENT_TURN, name, None, actor=self.human)
            out.append({"name": name, "role": a.role, "skills": list(a.skills),
                        "tokens_used": a.tokens_used, "turns": a.turns,
                        "recent": [t[2] for t in turns[-3:]]})
        return {"agents": out}

    def _cmd_tuples(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState(
            "tuples", self.tuples_snapshot(arg.strip() or None)))

    def _cmd_files(self, arg: str, ctx: CommandContext) -> CommandResult:
        return CommandResult(view=ViewState("files", self.files_snapshot()))

    def _cmd_review(self, arg: str, ctx: CommandContext) -> CommandResult:
        pid = arg.strip() or None
        return CommandResult(jobs=[
            Job(start=lambda obs: self.do_review(pid, obs),
                group="review", exclusive=True)
        ])

    def _cmd_discuss(self, arg: str, ctx: CommandContext) -> CommandResult:
        arg = arg.strip()
        if not arg:
            return CommandResult(view=ViewState(
                "discussion", self.discussion_snapshot()))
        parts = arg.split()
        if len(parts) < 2:
            return CommandResult(messages=[
                Notice("usage: /discuss <name1> <name2> [topic]", "error")
            ])
        name1, name2 = parts[0], parts[1]
        topic = " ".join(parts[2:])
        taken = set(self._task_workers) | set(self._llm_agents)
        if name1 == name2 or name1 in taken or name2 in taken:
            return CommandResult(messages=[
                Notice("agent names must be unique and not already running",
                       "error")
            ])
        d = Discussion(self.space, a_name=name1, b_name=name2,
                       llm=self.llm or LLMConfig(), topic=topic,
                       actor=self.human)
        self._llm_agents[name1] = d.a
        self._llm_agents[name2] = d.b
        self._discussions.append(d)
        return CommandResult(
            messages=[Notice(f"discussion {name1} ↔ {name2} started — "
                             "/agents to watch, /stop to end")],
            jobs=[Job(start=lambda obs: self._run_discussion(d, name1, name2, obs),
                      group=f"discuss-{name1}-{name2}")],
        )

    def discussion_snapshot(self, discussion: Discussion | None = None) -> dict:
        if discussion is None:
            discussion = self._discussions[-1] if self._discussions else None
        turns = self.space.read_all(AGENT_TURN, None, None, actor=self.human)
        if discussion is not None:
            names = {discussion.a.name, discussion.b.name}
            turns = [t for t in turns if t[1] in names]
            return {"names": (discussion.a.name, discussion.b.name),
                    "topic": discussion.topic, "turns": turns}
        return {"names": None, "topic": "", "turns": turns}

    def _cmd_stop(self, arg: str, ctx: CommandContext) -> CommandResult:
        name = arg.strip() or None
        if name is not None:
            w = self._task_workers.get(name)
            a = self._llm_agents.get(name)
            if w is None and a is None:
                return CommandResult(messages=[
                    Notice(f"no worker or agent {name!r} running", "warning")
                ])
            if w is not None:
                w.stop()
            if a is not None:
                a.stop()
            return CommandResult(messages=[
                Notice(f"stopping {name} (in-flight work finishes first)")
            ])
        if not self._task_workers and not self._llm_agents:
            return CommandResult(messages=[
                Notice("no workers or agents running", "warning")
            ])
        for w in self._task_workers.values():
            w.stop()
        for a in self._llm_agents.values():
            a.stop()
        return CommandResult(messages=[
            Notice("stopping workers and agents (in-flight work finishes first)")
        ])

    def _cmd_reset(self, arg: str, ctx: CommandContext) -> CommandResult:
        space = self.space
        if getattr(space, "_remote", False):
            return CommandResult(messages=[Notice(
                "reset is disabled on a remote board — the server owns the WAL "
                "(restart the server with --recover to reset)", "warning")])
        if not self._reset_armed:
            counts = {tag: len(space.read_all(tag, actor=self.human))
                      for tag in sorted(RESET_TAGS)}
            total = sum(counts.values())
            runs = 0
            if space.run_dir is not None:
                runs = max(0, len(prior_runs(space.run_dir.parent)) - 1)
            self._reset_armed = True
            return CommandResult(messages=[Notice(
                f"reset: {total} item(s) will be cleared (settings included), "
                f"{runs} prior run(s) archived — /reset again to confirm")])
        self._reset_armed = False
        for w in self._task_workers.values():
            w.stop()
        for a in self._llm_agents.values():
            a.stop()
        result = reset(space, actor=self.human)
        cleared = sum(result["cleared"].values())
        self.chat_mode = "human"
        if self._chat_agent is not None:
            self._chat_agent.mode = "human"
        self.stock = None
        if self._chat_agent is not None:
            self._chat_agent.stock = None
        return CommandResult(
            messages=[Notice(f"board reset — {cleared} item(s) cleared, "
                             f"{result['archived']} run(s) archived")],
            table=self._table(ctx.tag),
            view=ViewState("status", self.status_snapshot()),
        )

    # ── long-running operations ─────────────────────────────────────────

    async def do_chat(self, text: str, observer: Observer) -> None:
        try:
            agent = self.ensure_chat_agent()
            observer.on_view(ViewState(
                "chat", {"turns": self.chat_transcript(),
                         "pending": text, "thinking": True}))
            result = await agent.send(text)
            observer.on_view(ViewState(
                "chat", {"turns": self.chat_transcript()}, scroll_end=True))
            observer.on_state()
            observer.on_message(f"{result['tokens_used']:,} tokens · "
                                f"{result['api_calls']} call(s)")
        except Exception as e:
            observer.on_message(f"chat failed: {e}", level="error")

    async def do_architect(self, text: str, observer: Observer) -> None:
        try:
            observer.on_view(ViewState(
                "architect", {**self.architect_snapshot(),
                              "pending": text, "thinking": True}))
            result = await self._architect_agent.send(text)
            observer.on_view(ViewState(
                "architect", self.architect_snapshot(), scroll_end=True))
            observer.on_state()
            cost = (f"{result['tokens_used']:,} tokens · "
                    f"{result['api_calls']} call(s)")
            files = result.get("files") or []
            if files:
                cost += f" · {len(files)} file(s) → {self._record_files(files)}"
            if result.get("over_budget"):
                budget = self._architect_agent.llm.token_budget
                observer.on_message(
                    f"{cost} — over budget ({budget:,}); "
                    "cost is reported, not enforced",
                    level="warning")
            else:
                observer.on_message(cost)
        except Exception as e:
            observer.on_view(ViewState(
                "architect", {**self.architect_snapshot(),
                              "error": str(e)}, scroll_end=True))
            observer.on_message(f"architect failed: {e}", level="error")

    def _record_files(self, files: list[dict]) -> str:
        """Enforce board-level detection of the architect's file delivery.

        The substrate's ``file_created`` tuples are the raw event log (what
        ``/files`` shows); this mirrors each delivered file into the curated
        ``knowledge`` tag — deterministically, in code, never via the model
        — so a delivered file is visible where the human already navigates:
        the knowledge tab, the filter, wildcard search. It is an action
        record (provenance of a completed side effect), not proposed
        content, so it is written directly (not through the proposal gate)
        and attributed to the architect in the WAL.
        """
        board = Board(self.space, actor=self._architect_agent.name)
        ids = []
        for f in files:
            ids.append(board.insert(
                "knowledge", name=f["path"],
                context=f"architect {self._architect_agent.name}",
                text=f"file written by architect "
                     f"{self._architect_agent.name} ({f['bytes']} bytes); "
                     f"sandbox: {self._architect_agent.sandbox}"))
        return ", ".join(ids)

    async def do_plan(self, tid: str, observer: Observer) -> None:
        try:
            ids = await plan(self.space, tid=tid,
                             llm=self.llm or LLMConfig(), actor=self.human)
        except Exception as e:
            observer.on_message(f"plan failed: {e}", level="error")
            return
        observer.on_state()
        observer.on_message(f"planned {tid} → {', '.join(ids)}")

    async def do_review(self, pid: str | None, observer: Observer) -> None:
        try:
            results = await review(self.space, llm=self.llm or LLMConfig(),
                                   pid=pid, actor=self.human)
        except Exception as e:
            observer.on_message(f"review failed: {e}", level="error")
            return
        for line in results:
            observer.on_message(f"review: {line}")
        observer.on_state()

    async def _run_task_worker(self, name: str, worker: TaskWorker,
                                observer: Observer) -> None:
        def event(message: str) -> None:
            observer.on_message(message)
            observer.on_state()

        try:
            await worker.run(on_event=event)
        finally:
            self._task_workers.pop(name, None)

    async def _run_generic_agent(self, name: str, agent: GenericAgent,
                                 observer: Observer) -> None:
        def event(message: str) -> None:
            observer.on_message(message)
            observer.on_state()

        try:
            await agent.run(on_event=event)
        finally:
            self._llm_agents.pop(name, None)

    async def _run_discussion(self, discussion: Discussion,
                              name1: str, name2: str,
                              observer: Observer) -> None:
        def event(message: str) -> None:
            observer.on_message(message)
            observer.on_state()

        try:
            await discussion.run(on_event=event)
        finally:
            self._llm_agents.pop(name1, None)
            self._llm_agents.pop(name2, None)
            if discussion in self._discussions:
                self._discussions.remove(discussion)
