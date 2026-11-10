"""ABoard TUI — a keyboard-driven navigator for a tuple space of tasks,
knowledge, and skills.

Navigation is the point: a filter box narrows the current tag's tuples as you
type (client-side substring), arrow keys move the cursor and the detail pane
updates instantly, and the command line handles the rest (insert, delete,
chat). The substrate's exact+wildcard associative matching is still available
via ``/search``; the filter box is pure UX on top of the loaded board.

Sessions persist: launching resumes the latest board (:func:`aboard.session.open_space`)
and quitting closes (fsyncs) the WAL, so the board survives restarts.
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Markdown, Select

from tuple_space import BudgetExceeded
from tuple_space import TupleSpace


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


from . import config
from .board import Board
from .chat import ChatAgent, LLMConfig, MODES
from .models import COUNTER_TAG, TAGS, fields
from .proposals import apply, describe, pending, reject
from .session import load_stock, open_space, prior_runs, reset
from .workers import TaskWorker, plan


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
        # classic TUI contract: tab cycles the panels (select → filter →
        # table → command); the same actions the screen binds, made visible.
        Binding("tab", "app.focus_next", "Next panel"),
        Binding("shift+tab", "app.focus_previous", "Prev panel"),
        # vim-style cursor movement (arrows come from DataTable defaults)
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        # keyboard-only tag navigation: cycle + direct jumps (Textual names
        # [ and ] as left/right_square_bracket)
        Binding("right_square_bracket", "next_tag", "Next tag"),
        Binding("left_square_bracket", "prev_tag", "Prev tag"),
        Binding("1", "jump_tag(0)", show=False),
        Binding("2", "jump_tag(1)", show=False),
        Binding("3", "jump_tag(2)", show=False),
        # approve/reject the oldest pending proposal — one-key decisions
        Binding("a", "approve", "Approve"),
        Binding("r", "reject", "Reject"),
        # discoverability: the full command reference is one keypress away
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
                 chat_agent: ChatAgent | None = None):
        super().__init__()
        self.board = board if board is not None else Board(
            space if space is not None else TupleSpace())
        self.session_note = note       # shown as a toast on mount (see main())
        self.human = human             # this client's name (WAL attribution)
        self.chat_mode = chat_mode     # "human" (propose+approve) or "auto"
        self.stock = stock             # read-only stock mount (launch snapshot)
        self.llm = llm                 # endpoint settings; lazily used on /chat
        self._chat_agent = chat_agent  # injectable for deterministic tests
        self._tag = TAGS[0]           # the active tag
        self._detail_view: str | None = None   # transient pane view (below)
        self._task_workers: dict[str, TaskWorker] = {}   # name → worker (see /work)
        self._worker_seq = 0
        self._reset_armed = False        # /reset needs a second /reset to fire
        self._query = ""              # current filter text
        self._tuples: dict[str, tuple] = {}  # id -> tuple, for the active tag
        self._current_tid: str | None = None

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
        # id + every content field; cells are truncated (below), the detail
        # pane shows the full text — a task table that showed only ids hid
        # the actual work from view
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
        rows = (self.board.filter(self._tag, self._query) if self._query
                else self.board.all(self._tag))
        n = len(fields(self._tag))
        for t in rows:
            tid = t[1]
            self._tuples[tid] = t
            cells = [tid] + [self._preview(str(t[2 + i]), limit=48)
                             for i in range(n)]
            table.add_row(*cells, key=tid)
        # reloads must not yank the cursor to row 0 (a chat reply or worker
        # event refresh would otherwise change the highlight and clear the
        # detail pane); restore the same row when it still exists
        if previous_tid and previous_tid in table.rows:
            table.move_cursor(row=list(table.rows).index(previous_tid))
        self.sub_title = f"{len(rows)} {self._tag}"

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
        """Render into the detail pane. ``view`` names a transient view
        (chat / proposals / stock / search / status / help): a table reload's
        re-highlight must not overwrite it — a reply used to flash on the
        screen and vanish exactly this way. Only explicit cursor movement
        (a different row) returns the pane to item detail.
        ``scroll_end`` parks the pane at the bottom (chat: newest reply).
        """
        self._detail_view = view
        pane = self.query_one("#detail", DetailPane)
        self.query_one("#detail_body", Markdown).update(markdown)
        if scroll_end:
            pane.call_after_refresh(lambda: pane.scroll_end(animate=False))

    # ── events ─────────────────────────────────────────────────────────

    def on_select_changed(self, event: Select.Changed) -> None:
        """The tag selector. Only curated tags are accepted: a blank/null
        selection (Select can clear itself — e.g. on overlay dismissal when
        blank is allowed) is ignored and the display restored, rather than
        crashing on ``fields(Select.NULL)``. With ``allow_blank=False`` the
        widget cannot be cleared, but the guard stays defensive.
        """
        if event.select.id != "tag" or event.value == self._tag:
            return
        if event.value not in TAGS:
            event.select.value = self._tag      # restore the real tag display
            return
        self._switch_tag(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._query = event.value
            self._reload()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        tid = event.row_key.value if event.row_key else None
        # a transient view holds the pane: skip reload re-highlights of the
        # same row (and empty-table events) — the view stays until the
        # cursor explicitly moves to a different row
        if self._detail_view is not None and (tid is None or tid == self._current_tid):
            return
        self._current_tid = tid
        self._show_detail(tid)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter / click on a row = the explicit "show this tuple" action:
        force item detail, replacing any transient view (the highlight guard
        above cannot do that for the same row or an empty table)."""
        tid = event.row_key.value if event.row_key else None
        if tid is not None:
            self._current_tid = tid
            self._show_detail(tid)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cmd":
            self._run_command(event.value)
            event.input.value = ""
        elif event.input.id == "filter":
            # tab is tag-switching, not focus-cycling, so enter is the way
            # "back to the data" after filtering
            self.query_one("#table", DataTable).focus()

    # ── commands ────────────────────────────────────────────────────────

    def _run_command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("/"):
            self._chat(line)          # bare text is a chat message
            return
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if self._reset_armed and cmd != "reset":
            self._reset_armed = False   # any other command disarms the reset
        if cmd == "add":
            self._add(arg)
        elif cmd == "del":
            self._delete()
        elif cmd == "chat":
            self._chat(arg)
        elif cmd == "search":
            self._search(arg)
        elif cmd == "clear":
            self._clear(arg)
        elif cmd == "proposals":
            self._show_proposals()
        elif cmd == "approve":
            self._decide(arg.strip() or None, approve=True)
        elif cmd == "reject":
            self._decide(arg.strip() or None, approve=False)
        elif cmd == "import":
            self._import_item(arg)
        elif cmd == "mode":
            self._mode_cmd(arg)
        elif cmd == "stock":
            self._stock_cmd(arg)
        elif cmd == "plan":
            self._plan_cmd(arg)
        elif cmd == "work":
            self._work_cmd(arg)
        elif cmd == "stop":
            self._stop_workers()
        elif cmd == "status":
            self._show_status()
        elif cmd == "reset":
            self._reset_cmd()
        elif cmd == "help":
            self.action_show_help()
        else:
            self.notify(f"unknown command /{cmd} — /help lists all",
                        severity="error")

    def _add(self, arg: str) -> None:
        tag, _, rest = arg.partition(" ")
        if tag not in TAGS:
            self.notify(f"usage: /add <{'|'.join(TAGS)}> …", severity="error")
            return
        fs = fields(tag)
        parts = [p.strip() for p in rest.split("|")]
        parts += [""] * (len(fs) - len(parts))  # pad optional fields
        values = dict(zip(fs, parts[:len(fs)]))
        try:
            tid = self.board.insert(tag, **values)
        except (TypeError, ValueError) as e:
            self.notify(str(e), severity="error")
            return
        self._tag = tag
        self.query_one("#tag", Select).value = tag
        self._setup_columns()
        self._reload()
        self.notify(f"added {tag} {tid}")

    def _delete(self) -> None:
        if not self._current_tid:
            self.notify("nothing selected", severity="warning")
            return
        tag, tid = self._tag, self._current_tid
        self.board.remove(tag, tid)
        self._reload()
        self.notify(f"removed {tag} {tid}")

    def _search(self, arg: str) -> None:
        # Exact + wildcard associative search (the substrate's query), shown
        # in the detail pane as a distinct "search result" view.
        tag, _, rest = arg.partition(" ")
        if tag not in TAGS:
            self.notify(f"usage: /search <{'|'.join(TAGS)}> <text>",
                        severity="error")
            return
        # lock the key field, wildcard the rest
        key = fields(tag)[0]
        rows = self.board.search(tag, **{key: rest})
        if not rows:
            self._detail_update(f"## search: {tag}\n\nno exact match for `{rest!r}`",
                                view="search")
            return
        lines = [f"## search: {tag} matches `{rest!r}` ({len(rows)})"]
        for t in rows:
            lines.append(f"- `{t[1]}`  {self._preview(t[2])}")
        self._detail_update("\n".join(lines), view="search")

    def _clear(self, arg: str) -> None:
        tag = arg.strip() or self._tag
        if tag not in TAGS:
            self.notify(f"unknown tag {tag!r}", severity="error")
            return
        for t in self.board.all(tag):
            self.board.remove(tag, t[1])
        self._reload()
        self.notify(f"cleared {tag}")

    def _show_proposals(self) -> None:
        """The pending-proposals view — the human-driven inbox."""
        ps = pending(self.board.space)
        if not ps:
            self._detail_update("## proposals\n\n(none pending — ask the agent to "
                                "propose, or /add directly)", view="proposals")
            return
        lines = [f"## proposals ({len(ps)} pending)",
                 "`/approve` applies the oldest, `/approve p<N>` a specific "
                 "one, `/reject` discards."]
        lines.extend(f"- {describe(t)}" for t in ps)
        self._detail_update("\n".join(lines), view="proposals")

    def _import_item(self, arg: str) -> None:
        """/import <tag> <id> — copy a stock item into the board (human-only)."""
        if not self.stock:
            self.notify("no stock mounted — /stock <runs root> to mount",
                        severity="warning")
            return
        tag, _, tid = arg.strip().partition(" ")
        if tag not in TAGS or not tid:
            self.notify("usage: /import <{'|'.join(TAGS)}> <id>",
                        severity="error")
            return
        try:
            new_id = self.board.import_from(self.stock, tag, tid)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        self._tag = tag
        self.query_one("#tag", Select).value = tag
        self._setup_columns()
        self._reload()
        self.notify(f"imported {tag} {tid} → {new_id}")

    def _mode_cmd(self, arg: str) -> None:
        """/mode [human|auto] — show or switch the chat agent's mode.

        A runtime, board-scoped setting, not a launch flag: the choice is
        stored in the space (config tuple) so resume restores it, and it
        applies live — the next chat turn already runs in the new mode.
        """
        arg = arg.strip()
        if not arg:
            self.notify(f"chat mode: {self.chat_mode} — switch with "
                        f"{' or '.join(f'/mode {m}' for m in MODES)}")
            return
        if arg not in MODES:
            self.notify(f"mode must be one of {MODES}", severity="error")
            return
        self.chat_mode = arg
        config.store(self.board.space, config.MODE, arg, actor=self.human)
        if self._chat_agent is not None:
            self._chat_agent.mode = arg
        self.notify("chat mode: auto — the agent now writes directly"
                    if arg == "auto" else
                    "chat mode: human — the agent proposes; /approve applies")

    def _stock_cmd(self, arg: str) -> None:
        """/stock [path|off] — mount, show, or unmount the stock at runtime.

        Like the mode, the mount is stored in the space: resume re-mounts
        it. Mounting re-takes the snapshot, so ``/stock <path>`` is also the
        refresh.
        """
        arg = arg.strip()
        if not arg:
            self._show_stock()
            return
        if arg == "off":
            config.clear(self.board.space, config.STOCK, actor=self.human)
            self.stock = None
            if self._chat_agent is not None:
                self._chat_agent.stock = None
            self.notify("stock unmounted")
            return
        if not Path(arg).is_dir():
            self.notify(f"no such directory: {arg}", severity="error")
            return
        items = load_stock(arg)      # lenient: warns + empty on corruption
        self.stock = items or None
        config.store(self.board.space, config.STOCK, arg, actor=self.human)
        if self._chat_agent is not None:
            self._chat_agent.stock = items or None
        self.notify(f"stock mounted: {len(items)} item(s) — "
                    "/import <tag> <id> to copy")

    def _show_stock(self) -> None:
        """The /stock view — what is mounted and how to use it."""
        if not self.stock:
            self._detail_update("## stock\n\n(none mounted — `/stock <runs root>` "
                                "to mount a read-only board of common "
                                "knowledge)", view="stock")
            return
        path = config.get(self.board.space, config.STOCK, actor=self.human)
        counts = ", ".join(
            f"{sum(1 for t in self.stock if t[0] == tag)} {tag}"
            for tag in TAGS)
        self._detail_update(
            f"## stock\n\n`{path}`\n\n{len(self.stock)} item(s): {counts}\n\n"
            "`/import <tag> <id>` copies one item into the board",
            view="stock")

    # ── task execution: plan / work / stop / status ─────────────────────

    def _plan_cmd(self, arg: str) -> None:
        """/plan [id] — decompose a task into subtasks (needs the endpoint).

        Without an id, the highlighted task (current tag = task). The parent
        is claimed exactly-once; a failed plan re-posts it (no work lost).
        Runs in a worker — the LLM call is slow.
        """
        tid = arg.strip() or (self._current_tid if self._tag == TAGS[0] else None)
        if not tid:
            self.notify("highlight a task row, or: /plan <task id>",
                        severity="warning")
            return
        self.run_worker(self._do_plan(tid), thread=False,
                        exclusive=True, group="plan")

    async def _do_plan(self, tid: str) -> None:
        try:
            ids = await plan(self.board.space, tid=tid,
                             llm=self.llm or LLMConfig(), actor=self.human)
        except Exception as e:
            self.notify(f"plan failed: {e}", severity="error")
            return
        self._reload()
        self.notify(f"planned {tid} → {', '.join(ids)}")

    def _work_cmd(self, arg: str) -> None:
        """/work [n] — launch n worker agents (default 1).

        Each claims tasks atomically (exactly-once), executes them, and
        proposes the results as knowledge adds — you approve them with
        /approve, like any other proposal. /stop stops them all.
        """
        try:
            n = max(1, int(arg.strip() or 1))
        except ValueError:
            self.notify("usage: /work [n]", severity="error")
            return
        started = []
        for _ in range(n):
            name = f"w{self._worker_seq}"
            self._worker_seq += 1
            w = TaskWorker(self.board.space, name=name,
                          llm=self.llm or LLMConfig())
            self._task_workers[name] = w
            self.run_worker(w.run(on_event=self._worker_event),
                            thread=False, group=f"worker-{name}")
            started.append(name)
        self.notify(f"workers started: {', '.join(started)} — "
                    "claim → execute → propose; /approve applies, /stop halts")

    def _decide(self, pid, *, approve: bool) -> None:
        """One decision, shared by /approve, /reject, `a`, `r` — then refresh
        the table AND the live view (a stale proposals list after approving
        used to look like the approve had done nothing)."""
        if approve:
            msg = apply(self.board.space, self.board, pid, actor=self.human)
        else:
            msg = reject(self.board.space, pid, actor=self.human)
        self.notify(msg)
        self._reload()
        self._refresh_view()

    def _refresh_view(self) -> None:
        """Re-render the active transient view so it never goes stale."""
        if self._detail_view == "proposals":
            self._show_proposals()
        elif self._detail_view == "status":
            self._show_status()

    def action_approve(self) -> None:
        self._decide(None, approve=True)

    def action_reject(self) -> None:
        self._decide(None, approve=False)

    def _worker_event(self, message: str) -> None:
        """Toast + refresh on every worker state change (claims empty the
        task table as work happens; proposals land while you watch)."""
        self.notify(message)
        self._reload()
        self._refresh_view()

    def _stop_workers(self) -> None:
        if not self._task_workers:
            self.notify("no workers running", severity="warning")
            return
        for w in self._task_workers.values():
            w.stop()
        self.notify("stopping workers (in-flight tasks finish first)")

    def _reset_cmd(self) -> None:
        """`/reset` — factory reset, twice-confirmed.

        Clears everything (items, chat, proposals, settings — mode and stock
        mount reset to defaults), stops workers, and archives prior runs so
        the reset persists across restarts: resume skips empty runs, so
        clearing alone would be undone by the next launch. Prior runs land
        under `<runs root>/archive/` — moving one back restores that board.
        """
        space = self.board.space
        if not self._reset_armed:
            counts = {tag: len(space.read_all(tag, actor=self.human))
                      for tag in (*TAGS, "chat", "proposal", "config", COUNTER_TAG)}
            total = sum(counts.values())
            runs = 0
            if space.run_dir is not None:
                runs = max(0, len(prior_runs(space.run_dir.parent)) - 1)
            self._reset_armed = True
            self.notify(
                f"reset: {total} item(s) will be cleared (settings included), "
                f"{runs} prior run(s) archived — /reset again to confirm")
            return
        self._reset_armed = False
        for w in self._task_workers.values():
            w.stop()
        result = reset(space, actor=self.human)
        cleared = sum(result["cleared"].values())
        self.chat_mode = "human"          # settings reset to defaults
        if self._chat_agent is not None:
            self._chat_agent.mode = "human"
        self.stock = None
        if self._chat_agent is not None:
            self._chat_agent.stock = None
        self._reload()
        self._detail_update(
            "## board reset\n\n"
            f"- cleared: {cleared} item(s) ({', '.join(f'{k} {v}' for k, v in result['cleared'].items() if v)})\n"
            f"- prior runs archived: {result['archived']} (under `archive/` — "
            "move one back to restore it)\n"
            "- settings back to defaults (human mode, no stock mount)",
            view="status")
        self.notify(f"board reset — {cleared} item(s) cleared, "
                    f"{result['archived']} run(s) archived")

    # the command reference — /help or `?` from anywhere

    HELP = """## help

**keys**

- `/` filter, `enter` returns to the list · `:` command line
- `tab` / `shift+tab` move between panels (select → filter → table → detail → command)
- `arrows` / `j` / `k` move the cursor (detail follows); with the detail pane
  focused, `arrows` / `j` / `k` / `pgup` / `pgdn` scroll it (long items, chat)
- `enter` on a row = show that tuple in full (replaces any view)
- `]` / `[` cycle tag · `1` / `2` / `3` jump to task / knowledge / skill
- `n` new item · `d` delete the highlighted item · `?` this help · `ctrl+q` quit
- `a` approve · `r` reject — the oldest pending proposal, one keystroke

**commands**

- `/add task …` · `/add knowledge name | context | text` · `/add skill …`
- `/search <tag> <text>` — exact+wildcard associative search
- `/clear [tag]` — remove every item of a tag
- `/chat <text>` (or any bare text) — talk to the agent; `/chat` re-opens the
  transcript · `/mode [human|auto]` — propose-then-approve vs direct writes
- `/proposals` · `/approve [pN]` · `/reject [pN]` — the proposal inbox
- `/plan [id]` — decompose a task into 2–5 subtasks
- `/work [n]` — launch worker agents (claim → execute → propose) · `/stop`
- `/status` — tasks/proposals pending, workers at a glance
- `/stock [path|off]` — mount shared knowledge read-only ·
  `/import <tag> <id>` — copy a stock item onto the board
- `/reset` — factory reset (clears items, chat, proposals AND settings;
  prior runs archived under `archive/`, type it twice)
- `/help` — this view
"""

    def action_show_help(self) -> None:
        self._detail_update(self.HELP, view="help")

    def _show_status(self) -> None:
        """The /status view — the board at a glance."""
        space = self.board.space
        tasks = len(space.read_all("task", None, None, actor=self.human))
        props = len(space.read_all("proposal", None, None, None, None,
                                   actor=self.human))
        lines = ["## status",
                 f"- tasks pending: {tasks}",
                 f"- proposals pending: {props}"]
        if self._task_workers:
            lines.append("- workers:")
            lines.extend(f"  - {w.name}: {w.tasks_done} done, "
                         f"{w.tokens_used:,} tokens"
                         for w in self._task_workers.values())
        else:
            lines.append("- workers: none (/work n starts some)")
        lines.append(f"- chat mode: {self.chat_mode} (/mode to switch)")
        self._detail_update("\n".join(lines), view="status")

    def _chat(self, text: str) -> None:
        """Send a chat turn in a background worker (never block the UI).

        Exclusive per group: a new turn cancels an in-flight one.
        With no argument, re-open the transcript (the chat tuples).
        """
        if not text.strip():
            if not self.board.space.read_all("chat", None, None, actor=self.human):
                self.notify("no chat yet — /chat <text> to start", severity="warning")
                return
            self._detail_update(self._render_chat(), view="chat")
            return
        self.run_worker(self._do_chat(text), thread=False,
                        exclusive=True, group="chat")

    def _ensure_chat_agent(self) -> ChatAgent:
        if self._chat_agent is None:
            self._chat_agent = ChatAgent(
                self.board.space, human=self.human, mode=self.chat_mode,
                stock=self.stock,
                llm=self.llm if self.llm is not None else LLMConfig())
        return self._chat_agent

    async def _do_chat(self, text: str) -> None:
        """One conversational turn: write it, think, render the history, and
        refresh the board (the agent may have changed it via board_* tools)."""
        try:
            agent = self._ensure_chat_agent()
            self._detail_update(self._render_chat(pending=text, thinking=True),
                                view="chat")
            result = await agent.send(text)
            self._detail_update(self._render_chat(), view="chat", scroll_end=True)
            self._reload()   # board_add / board_claim may have changed the table
            self.notify(f"{result['tokens_used']:,} tokens · "
                        f"{result['api_calls']} call(s)")
        except BudgetExceeded as e:
            self.notify(str(e), severity="error")
        except Exception as e:
            self.notify(f"chat failed: {e}", severity="error")

    def _render_chat(self, *, pending: str | None = None,
                     thinking: bool = False) -> str:
        """The chat view for the detail pane — rendered from the space (the
        chat tuples ARE the transcript), not from any local buffer."""
        turns = self.board.space.read_all("chat", None, None, actor=self.human)
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
        self._delete()

    def action_next_tag(self) -> None:
        idx = TAGS.index(self._tag)
        self._switch_tag(TAGS[(idx + 1) % len(TAGS)])

    def action_prev_tag(self) -> None:
        idx = TAGS.index(self._tag)
        self._switch_tag(TAGS[(idx - 1) % len(TAGS)])

    def action_jump_tag(self, index: int) -> None:
        self._switch_tag(TAGS[index])

    def _switch_tag(self, tag: str) -> None:
        """All tag navigation funnels here: tab / shift+tab / 1-2-3 (and the
        Select). Closes a dangling dropdown so keyboard switching never
        strands a popup open.
        """
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

    def action_cursor_down(self) -> None:
        self.query_one("#table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#table", DataTable).action_cursor_up()


def main() -> None:
    ap = argparse.ArgumentParser(description="ABoard — Agents Board TUI")
    ap.add_argument("--run-root", default=None,
                    help="tuple-space runs root (default: ./.tuplespace/)")
    ap.add_argument("--fresh", action="store_true",
                    help="start a new empty board instead of resuming the latest")
    ap.add_argument("--endpoint", default=None,
                    help="join an existing HTTP board (not yet wired)")
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
        raise SystemExit("--endpoint is not yet wired; start an in-process board "
                         "instead (omit --endpoint).")
    space, note = open_space(args.run_root, fresh=args.fresh)
    board = Board(space, actor=args.name)   # the human's ops are attributed

    # runtime config (chat mode, stock mount) lives in the space — /mode and
    # /stock wrote it there in previous sessions, and this resume restores it
    # (config tuples replay like every other mutation). The CLI stays for
    # identity/endpoint wiring only.
    mode = config.get(space, config.MODE, actor=args.name)
    chat_mode = mode if mode in MODES else "human"
    if mode is not None and mode not in MODES:
        warnings.warn(f"ignoring invalid stored chat mode {mode!r}; using human")
    stock_path = config.get(space, config.STOCK, actor=args.name)
    stock = load_stock(stock_path) if stock_path else None

    app = BoardApp(board=board, note=note, human=args.name,
                   chat_mode=chat_mode, stock=stock,
                   llm=LLMConfig(model=args.model, base_url=args.base_url,
                                 api_key=args.api_key))
    try:
        app.run()
    finally:
        # fsync the WAL: the next launch resumes this session's board
        space.close()


if __name__ == "__main__":
    main()
