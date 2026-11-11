"""ABoard TUI — a keyboard-driven navigator for a tuple space of tasks,
knowledge, and skills.

This module is now only the Textual shell. The board state and command
semantics live in :class:`aboard.core.BoardService`; this shell owns view
state (active tag, filter query, cursor) and rendering.
"""

from __future__ import annotations

import argparse
import warnings

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Select

from tuple_space import TupleSpace

from . import config
from .board import Board, format_tuples
from .chat import LLMConfig
from .core import (INSPECT_PER_TAG, BoardService, CommandContext, Observer,
                   ViewState)
from .http_space import HttpSpace
from .models import TAGS, fields
from .proposals import describe
from .session import load_stock, open_space


class DetailPane(VerticalScroll):
    """The right-hand pane: scrollable and focusable (so long tuples, chat
    transcripts and proposal lists can actually be read — ``Markdown`` alone
    is a plain, unscrollable widget). ``j``/``k`` scroll here at widget
    level, so they beat the app-level cursor bindings while the pane has
    focus; arrows/pageup/pagedown/home/end come from the scroll container.
    """

    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
    ]


class _TuiObserver:
    """Maps BoardService events onto Textual widgets."""

    def __init__(self, app: "BoardApp"):
        self.app = app

    def on_message(self, message: str, *, level: str = "info") -> None:
        severity = {"info": "information", "warning": "warning",
                    "error": "error"}.get(level, "information")
        self.app.notify(message, severity=severity)

    def on_view(self, state: ViewState) -> None:
        self.app._show_view(state)

    def on_table(self, state) -> None:
        self.app._apply_table(state)

    def on_state(self) -> None:
        self.app._reload()
        self.app._refresh_view()


class BoardApp(App):
    """The full-screen ABoard application."""

    TITLE = "ABoard"
    SUB_TITLE = "Agents Board"

    # first launch lands on the board itself: arrows/j/k work immediately
    AUTO_FOCUS = "#table"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("/", "focus_filter", "Filter"),
        Binding(":", "focus_cmd", "Command"),
        Binding("n", "new_item", "New"),
        Binding("d", "delete_item", "Delete"),
        Binding("tab", "app.focus_next", "Next panel"),
        Binding("shift+tab", "app.focus_previous", "Prev panel"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("right_square_bracket", "next_tag", "Next tag"),
        Binding("left_square_bracket", "prev_tag", "Prev tag"),
        Binding("1", "jump_tag(0)", show=False),
        Binding("2", "jump_tag(1)", show=False),
        Binding("3", "jump_tag(2)", show=False),
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        Binding("question_mark", "show_help", "Help"),
    ]

    CSS = """
    #main { height: 1fr; }
    #nav { width: 55%; border-right: solid $accent; }
    #detail { width: 45%; padding: 1 2; }
    #filter { margin-bottom: 1; }
    #table { height: 1fr; }
    """

    def __init__(self, space: TupleSpace | None = None,
                 board: Board | None = None,
                 note: str | None = None,
                 *, human: str = "human",
                 chat_mode: str = "human",
                 stock: list[tuple] | None = None,
                 llm: LLMConfig | None = None,
                 chat_agent=None,
                 architect_agent=None,
                 service: BoardService | None = None):
        super().__init__()
        self.service = service if service is not None else BoardService(
            space, board, human=human, chat_mode=chat_mode, stock=stock,
            llm=llm, chat_agent=chat_agent, architect_agent=architect_agent)
        self.session_note = note       # shown as a toast on mount (see main())
        self._observer = _TuiObserver(self)

        # view-only state (the core owns the domain state)
        self._tag = TAGS[0]
        self._detail_view: str | None = None
        self._last_detail_markdown: str | None = None
        self._query = ""
        self._tuples: dict[str, tuple] = {}
        self._current_tid: str | None = None
        self._proposals: list[tuple] = []
        self._proposal_index = 0

    # ── service state exposed for the shell and tests ─────────────────

    @property
    def board(self) -> Board:
        return self.service.board

    @property
    def human(self) -> str:
        return self.service.human

    @property
    def chat_mode(self) -> str:
        return self.service.chat_mode

    @property
    def stock(self):
        return self.service.stock

    @property
    def _task_workers(self):
        return self.service.task_workers

    @property
    def _llm_agents(self):
        return self.service.llm_agents

    @property
    def _discussions(self):
        return self.service.discussions

    @property
    def _reset_armed(self):
        return self.service.reset_armed

    @property
    def _architect_agent(self):
        return self.service.architect_agent

    def _ensure_chat_agent(self):
        return self.service.ensure_chat_agent()

    # ── composition ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="nav"):
                yield Select([(t, t) for t in TAGS], value=self._tag,
                              allow_blank=False, type_to_search=False, id="tag")
                yield Input(placeholder="filter (type to narrow, enter to return)…",
                            id="filter")
                yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
            with DetailPane(id="detail"):
                yield Markdown("", id="detail_body")
        yield Input(placeholder="command — /help lists all · /add task … · /chat … · d deletes",
                    id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_columns()
        self._reload()
        if self.session_note:
            self.notify(self.session_note)

    # ── table helpers ───────────────────────────────────────────────────

    def _columns(self) -> list[str]:
        return ["id"] + list(fields(self._tag))

    def _setup_columns(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns(*self._columns())

    def _reload(self) -> None:
        table = self.query_one("#table", DataTable)
        previous_tid = self._current_tid
        table.clear()
        self._tuples = {}
        rows = self.service.list_items(self._tag, self._query)
        n = len(fields(self._tag))
        for t in rows:
            tid = t[1]
            self._tuples[tid] = t
            cells = [tid] + [self._preview(str(t[2 + i]), limit=48)
                             for i in range(n)]
            table.add_row(*cells, key=tid)
        if previous_tid and previous_tid in table.rows:
            table.move_cursor(row=list(table.rows).index(previous_tid))
        self.sub_title = self.service.table_state(
            self._tag, self._query).subtitle

    def _render_detail(self, tup: tuple) -> str:
        tag, tid = tup[0], tup[1]
        fs = fields(tag)
        lines = [f"## {tag}  `{tid}`"]
        for i, f in enumerate(fs):
            v = tup[2 + i]
            lines.append(f"**{f}**: {v if v else '—'}")
        return "\n\n".join(lines)

    def _show_detail(self, tid: str | None) -> None:
        tup = self._tuples.get(tid) if tid else None
        self._detail_update(self._render_detail(tup) if tup else "")

    def _detail_update(self, markdown: str, *, view: str | None = None,
                        scroll_end: bool = False) -> None:
        if markdown == self._last_detail_markdown and view == self._detail_view:
            return
        self._detail_view = view
        self._last_detail_markdown = markdown
        pane = self.query_one("#detail", DetailPane)
        self.query_one("#detail_body", Markdown).update(markdown)
        if scroll_end:
            pane.call_after_refresh(lambda: pane.scroll_end(animate=False))

    # ── events ─────────────────────────────────────────────────────────

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "tag" or event.value == self._tag:
            return
        if event.value not in TAGS:
            event.select.value = self._tag
            return
        self._switch_tag(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._query = event.value
            self._reload()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        tid = event.row_key.value if event.row_key else None
        if self._detail_view is not None and (tid is None or tid == self._current_tid):
            return
        self._current_tid = tid
        self._show_detail(tid)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        tid = event.row_key.value if event.row_key else None
        if tid is not None:
            self._current_tid = tid
            self._show_detail(tid)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cmd":
            self._run_command(event.value)
            event.input.value = ""
        elif event.input.id == "filter":
            self.query_one("#table", DataTable).focus()

    # ── command dispatch ────────────────────────────────────────────────

    def _ctx(self) -> CommandContext:
        return CommandContext(tag=self._tag, tid=self._current_tid,
                              proposal_pid=self._selected_proposal_pid())

    def _run_command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        result = self.service.handle(line, ctx=self._ctx())
        self._apply_result(result)

    def _apply_result(self, result) -> None:
        for notice in result.messages:
            self._observer.on_message(notice.message, level=notice.level)
        if result.table is not None:
            self._apply_table(result.table)
            if result.view is None:
                self._refresh_view()
        if result.view is not None:
            self._show_view(result.view)
        for job in result.jobs:
            self.run_worker(job.start(self._observer), thread=False,
                            exclusive=job.exclusive, group=job.group)

    def _apply_table(self, state) -> None:
        if state.tag != self._tag:
            self._tag = state.tag
            self.query_one("#tag", Select).value = state.tag
            self._setup_columns()
        self._reload()

    def _show_view(self, state: ViewState) -> None:
        renderer = getattr(self, f"_render_{state.name}", None)
        markdown = renderer(state.data) if renderer else ""
        self._detail_update(markdown, view=state.name,
                            scroll_end=state.scroll_end)

    def _refresh_view(self) -> None:
        name = self._detail_view
        if name == "proposals":
            self._show_view(ViewState("proposals",
                                      self.service.proposals_snapshot()))
        elif name == "status":
            self._show_view(ViewState("status",
                                      self.service.status_snapshot()))
        elif name == "discussion":
            self._show_view(ViewState("discussion",
                                      self.service.discussion_snapshot()))
        elif name == "files":
            self._show_view(ViewState("files",
                                      self.service.files_snapshot()))
        elif name == "architect":
            self._show_view(ViewState("architect",
                                      self.service.architect_snapshot()))
        elif name == "chat":
            self._show_view(ViewState(
                "chat", {"turns": self.service.chat_transcript()}))

    # ── renderers (shell-owned) ─────────────────────────────────────────

    def _render_chat(self, data: dict) -> str:
        turns = data.get("turns", [])
        pending = data.get("pending")
        thinking = data.get("thinking", False)
        lines = ["## chat"]
        for t in turns[-12:]:
            lines.append(f"**{t[1]}:** {t[2]}")
        if pending:
            lines.append(f"**{self.human}:** {pending}")
        if thinking:
            lines.append("*…thinking*")
        if len(lines) == 1:
            return "## chat\n\n(no turns yet — say something)"
        return "\n\n".join(lines)

    def _render_architect(self, data: dict) -> str:
        name = data.get("name")
        turns = data.get("turns", [])
        pending = data.get("pending")
        thinking = data.get("thinking", False)
        error = data.get("error")
        lines = [f"## architect: {name}" if name else "## architect"]
        for t in turns[-12:]:
            lines.append(f"**{t[2]}:** {t[3]}")
        if pending:
            lines.append(f"**{self.human}:** {pending}")
        if thinking:
            lines.append("*…thinking*")
        if error:
            lines.append(f"*architect failed: {error}*")
        if len(lines) == 1:
            return (f"## architect: {name}\n\n(no turns yet — say something)"
                    if name else
                    "## architect\n\n(none active — `/architect start <name> "
                    "<role> [skills…]` to start a file-capable agent)")
        return "\n\n".join(lines)

    def _render_proposals(self, data: dict) -> str:
        ps = data.get("proposals", [])
        reviews = data.get("reviews", {})
        self._proposals = ps
        if not ps:
            self._proposal_index = 0
            return "## proposals\n\n(none pending — ask the agent to propose, or /add directly)"
        self._proposal_index = max(0, min(self._proposal_index, len(ps) - 1))
        lines = [f"## proposals ({len(ps)} pending)",
                 "`j`/`k` select · `a` approve · `r` reject the marked proposal",
                 "`/approve [pN]` / `/reject [pN]` target one explicitly"]
        for i, t in enumerate(ps):
            marker = "▶" if i == self._proposal_index else " "
            lines.append(f"{marker} {describe(t)}")
            rv = reviews.get(t[1]) or []
            if rv:
                lines.append(f"    ↳ review: {rv[-1][2]} — {rv[-1][3]}")
        return "\n".join(lines)

    def _render_search(self, data: dict) -> str:
        tag, query, rows = data["tag"], data["query"], data["rows"]
        if not rows:
            return f"## search: {tag}\n\nno exact match for `{query!r}`"
        lines = [f"## search: {tag} matches `{query!r}` ({len(rows)})"]
        for t in rows:
            lines.append(f"- `{t[1]}`  {self._preview(t[2])}")
        return "\n".join(lines)

    def _render_stock(self, data: dict) -> str:
        if not data.get("mounted"):
            return ("## stock\n\n(none mounted — `/stock <runs root>` "
                    "to mount a read-only board of common knowledge)")
        counts = ", ".join(f"{data['counts'][tag]} {tag}" for tag in TAGS)
        return (f"## stock\n\n`{data['path']}`\n\n{data['count']} item(s): "
                f"{counts}\n\n`/import <tag> <id>` copies one item into the board")

    def _render_agent_defs(self, data: dict) -> str:
        defs = data.get("defs", [])
        running = data.get("running", set())
        if not defs:
            return "## agents\n\n(none defined — `/agent add <name> <role> [skills…]`)"
        lines = ["## agents"]
        for d in defs:
            name, role, skills = d[1], d[2], d[3]
            state = "running" if name in running else "idle"
            skills_s = f" · skills: {', '.join(skills)}" if skills else ""
            lines.append(f"- `{name}` — {role}{skills_s} ({state})")
        return "\n".join(lines)

    def _render_llm_agents(self, data: dict) -> str:
        agents_list = data.get("agents", [])
        if not agents_list:
            return ("## agents\n\n(none running — `/spawn <name> <role> "
                    "[skills…]` to launch a generic agent)")
        lines = ["## agents"]
        for a in agents_list:
            skills_s = f" · skills: {', '.join(a['skills'])}" if a["skills"] else ""
            lines.append(f"- `{a['name']}` — {a['role']}{skills_s} · "
                         f"{a['tokens_used']:,} tokens · {a['turns']} turn(s)")
            for text in a["recent"]:
                lines.append(f"  - {text}")
        return "\n".join(lines)

    def _render_tuples(self, data: dict) -> str:
        if data.get("tag"):
            tag, tuples = data["tag"], data["tuples"]
            if not tuples:
                return f"## tuples: {tag}\n\n(no tuples)"
            return (f"## tuples: {tag}\n\n"
                    f"{format_tuples(tuples, limit=INSPECT_PER_TAG)}")
        groups, total = data.get("groups", {}), data.get("total", 0)
        if not groups:
            return "## tuples\n\n(space is empty)"
        sections = [f"## tuples ({total} total)"]
        for name in sorted(groups):
            items = groups[name]
            sections.append(f"### {name} ({len(items)})\n\n"
                            f"{format_tuples(items, limit=INSPECT_PER_TAG)}")
        return "\n\n".join(sections)

    def _render_files(self, data: dict) -> str:
        projects, files, errors = data["projects"], data["files"], data["errors"]
        lines = ["## files"]
        if projects:
            lines.append("### projects")
            for p in projects:
                lines.append(f"- `{p[1]}` → {p[2]}")
        if files:
            lines.append("### created")
            for f in files:
                lines.append(f"- `{f[1]}` ({f[2]} bytes)")
        if errors:
            lines.append("### errors")
            for f in errors:
                lines.append(f"- `{f[1]}`: {f[2]}")
        if not projects and not files and not errors:
            lines.append("(none — `/architect start <name> <role> [skills…]` "
                         "then `/architect <text>` to scaffold)")
        return "\n".join(lines)

    def _render_status(self, data: dict) -> str:
        lines = ["## status",
                 f"- tasks pending: {data['tasks']}",
                 f"- proposals pending: {data['proposals']}"]
        agent_defs = data["agent_defs"]
        if agent_defs:
            lines.append("- agents:")
            for d in agent_defs:
                name, role, skills = d[1], d[2], d[3]
                state = "running" if name in self._task_workers else "idle"
                skills_s = f" · skills: {', '.join(skills)}" if skills else ""
                lines.append(f"  - {name} ({role}{skills_s}) — {state}")
        else:
            lines.append("- agents: none (/agent add …)")
        if data["workers"]:
            lines.append("- workers:")
            lines.extend(f"  - {w['name']}: {w['tasks_done']} done, "
                         f"{w['tokens_used']:,} tokens"
                         for w in data["workers"])
        else:
            lines.append("- workers: none (/work n starts some)")
        if data["generic_agents"]:
            lines.append("- generic agents:")
            lines.extend(f"  - {a['name']} ({a['role']}): {a['turns']} turn(s), "
                         f"{a['tokens_used']:,} tokens"
                         for a in data["generic_agents"])
        if data["architect"]:
            a = data["architect"]
            skills_s = f" · skills: {', '.join(a['skills'])}" if a["skills"] else ""
            lines.append("- architect:")
            lines.append(f"  - {a['name']} ({a['role']}{skills_s}): "
                         f"{a['turns']} turn(s), {a['tokens_used']:,} tokens")
        lines.append(f"- chat mode: {data['chat_mode']} (/mode to switch)")
        return "\n".join(lines)

    def _render_help(self, data: dict) -> str:
        ctx = [
            f"- chat mode: {data['chat_mode']} (`/mode` to switch)",
            f"- stock: {'mounted' if data['stock'] else 'none'} (`/stock` to mount)",
            f"- proposals pending: {data['proposals']}",
        ]
        return self.HELP + "\n\n**current board**\n\n" + "\n".join(ctx)

    def _render_discussion(self, data: dict) -> str:
        names = data.get("names")
        topic = data.get("topic", "")
        turns = data.get("turns", [])
        title = (f"## discussion: {names[0]} ↔ {names[1]}"
                 if names else "## discussion")
        header = title
        if topic:
            header += f"\n\ntopic: {topic}"
        if not turns:
            return header + "\n\n(no turns yet — " \
                "`/discuss <name1> <name2> [topic]` to start two agents taking turns)"
        body = "\n\n---\n\n".join(
            f"### {t[1]} · turn {i}\n\n{t[2]}"
            for i, t in enumerate(turns, 1))
        return header + "\n\n" + body

    @staticmethod
    def _preview(text: str, limit: int = 60) -> str:
        text = str(text).replace("\n", " ")
        return text[:limit] + ("…" if len(text) > limit else "")

    # ── actions (keybindings) ───────────────────────────────────────────

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_cmd(self) -> None:
        self.query_one("#cmd", Input).focus()

    def action_new_item(self) -> None:
        cmd = self.query_one("#cmd", Input)
        cmd.value = f"/add {self._tag} "
        cmd.focus()

    def action_delete_item(self) -> None:
        self._run_command("/del")

    def action_next_tag(self) -> None:
        idx = TAGS.index(self._tag)
        self._switch_tag(TAGS[(idx + 1) % len(TAGS)])

    def action_prev_tag(self) -> None:
        idx = TAGS.index(self._tag)
        self._switch_tag(TAGS[(idx - 1) % len(TAGS)])

    def action_jump_tag(self, index: int) -> None:
        self._switch_tag(TAGS[index])

    def _switch_tag(self, tag: str) -> None:
        if tag == self._tag:
            return
        sel = self.query_one("#tag", Select)
        if sel.expanded:
            sel.expanded = False
        self._tag = tag
        sel.value = tag
        self._query = ""
        self.query_one("#filter", Input).value = ""
        self._setup_columns()
        self._reload()

    def _selected_proposal_pid(self) -> str | None:
        if (self._detail_view == "proposals"
                and 0 <= self._proposal_index < len(self._proposals)):
            return self._proposals[self._proposal_index][1]
        return None

    def action_approve(self) -> None:
        pid = self._selected_proposal_pid()
        self._run_command("/approve" if pid is None else f"/approve {pid}")

    def action_reject(self) -> None:
        pid = self._selected_proposal_pid()
        self._run_command("/reject" if pid is None else f"/reject {pid}")

    def action_cursor_down(self) -> None:
        if self._detail_view == "proposals" and self._proposals:
            self._proposal_index = min(self._proposal_index + 1,
                                       len(self._proposals) - 1)
            self._show_view(ViewState("proposals",
                                      self.service.proposals_snapshot()))
            return
        self.query_one("#table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self._detail_view == "proposals" and self._proposals:
            self._proposal_index = max(self._proposal_index - 1, 0)
            self._show_view(ViewState("proposals",
                                      self.service.proposals_snapshot()))
            return
        self.query_one("#table", DataTable).action_cursor_up()

    def action_show_help(self) -> None:
        self._show_view(ViewState("help", self.service.help_snapshot()))

    HELP = """## help

**first run**

- `/add task …` — put work on the board
- `/plan` — split the highlighted task into subtasks (optional)
- `/work 2` — launch 2 workers (claim → execute → propose)
- `/proposals` — see what they proposed
- `a` / `r` — approve / reject the marked proposal
- `/chat hello` — talk to the board's agent

**keys**

- `/` filter, `enter` returns to the list · `:` command line
- `tab` / `shift+tab` move between panels (select → filter → table → detail → command)
- `arrows` / `j` / `k` move the cursor (detail follows); with the detail pane
  focused, `arrows` / `j` / `k` / `pgup` / `pgdn` scroll it (long items, chat)
- `enter` on a row = show that tuple in full (replaces any view)
- `]` / `[` cycle tag · `1` / `2` / `3` jump to task / knowledge / skill
- `n` new item · `d` delete the highlighted item · `?` this help · `ctrl+q` quit
- `a` approve · `r` reject — in the proposals view: the marked proposal;
  otherwise the oldest pending proposal

**commands**

- `/add task …` · `/add knowledge name | context | text` · `/add skill …`
- `/del` — delete the highlighted item (same as `d`)
- `/search <tag> <text>` — exact+wildcard associative search
- `/clear [tag]` — remove every item of a tag
- `/chat <text>` (or any bare text) — talk to the agent; `/chat` re-opens the
  transcript · `/mode [human|auto]` — show/switch mode (propose-then-approve
  vs direct writes)
- `/proposals` — the proposal inbox (`j`/`k` select, `a`/`r` decide)
- `/approve [pN]` · `/reject [pN]` — decide a specific proposal
- `/plan [id]` — decompose a task into 2–5 subtasks
- `/work [n]` — launch anonymous workers (claim → execute → propose) ·
  `/stop [name]` — stop one worker or all
- `/agent [list]` · `/agent add <name> <role> [skills…]` ·
  `/agent start <name>` — named, role-specialized workers (skills gate task
  claims)
- `/spawn <name> <role> [skills…]` — launch a generic LLM agent coordinating
  on ANY tuples · `/agents` — the generic-agents panel (transcript, budget,
  skills)
- `/architect` — re-open the active architect transcript ·
  `/architect start <name> <role> [skills…]` — start/resume a file-capable
  agent · `/architect <text>` — talk to it (navigates the tuple space and
  writes files via `create_file`/`scaffold_files`) · `/files` — what agents
  have scaffolded
- `/review [pN]` — a reviewer writes an approve/reject/abstain verdict per
  pending proposal (recommendation only — `/approve` still decides)
- `/discuss <name1> <name2> [topic]` — two named agents take turns through the
  tuple space · `/discuss` re-opens the interleaved transcript
- `/tag <task_id> <skill>` — require a skill for a task
- `/dep <task_id> <dep1> [dep2…]` — declare task dependencies
- `/tuples [tag]` — read-only raw view of the space (one tag, or all grouped)
- `/status` — tasks/proposals/agents/workers at a glance
- `/stock [path|off]` — mount shared knowledge read-only; `/stock` shows the
  mount · `/import <tag> <id>` — copy a stock item onto the board
- `/reset` — factory reset (clears items, chat, proposals AND settings;
  prior runs archived under `archive/`, type it twice)
- `/help` — this view
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="ABoard — Agents Board TUI")
    ap.add_argument("--run-root", default=None,
                    help="tuple-space runs root (default: ./.tuplespace/)")
    ap.add_argument("--fresh", action="store_true",
                    help="start a new empty board instead of resuming the latest")
    ap.add_argument("--endpoint", default=None,
                    help="join an existing HTTP board (a tuple_space.server URL)")
    ap.add_argument("--name", default="human",
                    help="your participant name (WAL attribution, chat speaker)")
    ap.add_argument("--model", default=LLMConfig.model,
                    help="chat model (OpenAI-compatible endpoint)")
    ap.add_argument("--base-url", default=LLMConfig.base_url,
                    help="chat endpoint base URL")
    ap.add_argument("--api-key", default=LLMConfig.api_key,
                    help="chat endpoint API key")
    args = ap.parse_args()

    if args.endpoint:
        if args.fresh or args.run_root:
            warnings.warn(
                "--fresh and --run-root are ignored with --endpoint (the "
                "server owns the board's persistence)", UserWarning,
                stacklevel=2)
        space = HttpSpace(args.endpoint)
        note = f"joined board at {args.endpoint}"
    else:
        space, note = open_space(args.run_root, fresh=args.fresh)
    board = Board(space, actor=args.name)

    mode = config.get(space, config.MODE, actor=args.name)
    chat_mode = mode if mode in ("human", "auto") else "human"
    if mode is not None and mode not in ("human", "auto"):
        warnings.warn(f"ignoring invalid stored chat mode {mode!r}; using human")
    stock_path = config.get(space, config.STOCK, actor=args.name)
    stock = load_stock(stock_path) if stock_path else None

    service = BoardService(
        board=board, human=args.name, chat_mode=chat_mode, stock=stock,
        llm=LLMConfig(model=args.model, base_url=args.base_url,
                      api_key=args.api_key))
    app = BoardApp(service=service, note=note)
    try:
        app.run()
    finally:
        space.close()


if __name__ == "__main__":
    main()
