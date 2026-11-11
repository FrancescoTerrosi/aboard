"""ABoard web shell — a local browser GUI on top of :class:`aboard.core.BoardService`.

This module is deliberately thin. It owns no board logic; it translates
``BoardService`` reads/results into JSON and streams long-running job
progress to the browser with SSE.

Run with:

    python -m aboard.web --run-root ./.tuplespace --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import warnings
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tuple_space import TupleSpace

from . import config
from .board import Board
from .chat import LLMConfig
from .core import (BoardService, CommandContext, CommandResult, Job,
                   Observer, ViewState)
from .http_space import HttpSpace
from .models import TAGS
from .session import load_stock, open_space

WEB_DIR = Path(__file__).with_name("web")


def _to_json(obj: Any) -> Any:
    """Convert tuples/dataclasses into JSON-safe dicts/lists."""
    if isinstance(obj, tuple):
        return [_to_json(v) for v in obj]
    if isinstance(obj, list):
        return [_to_json(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_json(getattr(obj, k))
                for k in obj.__dataclass_fields__}
    return obj


class _WebObserver:
    """Observer that writes service events into an asyncio queue for SSE."""

    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def on_message(self, message: str, *, level: str = "info") -> None:
        self.queue.put_nowait({"type": "message", "message": message,
                               "level": level})

    def on_view(self, state: ViewState) -> None:
        self.queue.put_nowait({"type": "view", "state": {
            "name": state.name, "data": _to_json(state.data),
            "scroll_end": state.scroll_end}})

    def on_table(self, state) -> None:
        self.queue.put_nowait({"type": "table", "state": _to_json(state)})

    def on_state(self) -> None:
        self.queue.put_nowait({"type": "state"})


class JobRunner:
    """In-memory job manager for the local web shell.

    Each ``Job`` from the service becomes an asyncio task plus an SSE queue.
    Nothing is persisted beyond the board; restarting the web process loses
    only the transient job streams (the board and transcripts survive).
    """

    def __init__(self):
        self._counter = 0
        self._queues: dict[str, asyncio.Queue] = {}
        self._status: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, job: Job) -> str:
        jid = f"j{self._counter}"
        self._counter += 1
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[jid] = queue
        self._status[jid] = {"status": "running"}

        async def run():
            observer = _WebObserver(queue)
            try:
                await job.start(observer)
                self._status[jid]["status"] = "done"
            except Exception as e:  # surface, never take the server down
                self._status[jid]["status"] = "error"
                self._status[jid]["error"] = str(e)
            finally:
                await queue.put({"type": "done"})

        self._tasks[jid] = asyncio.create_task(run())
        return jid

    def get_queue(self, jid: str) -> asyncio.Queue | None:
        return self._queues.get(jid)

    def status(self, jid: str) -> dict:
        if jid not in self._status:
            raise HTTPException(status_code=404, detail="no such job")
        return self._status[jid]


class CommandRequest(BaseModel):
    line: str
    tag: str = TAGS[0]
    tid: str | None = None
    proposal_pid: str | None = None


def create_app(service: BoardService | None = None,
               runner: JobRunner | None = None) -> FastAPI:
    service = service or BoardService()
    runner = runner or JobRunner()

    app = FastAPI(title="ABoard")

    # ── JSON reads ─────────────────────────────────────────────────────

    @app.get("/api/items/{tag}")
    async def items(tag: str, query: str = ""):
        if tag not in TAGS:
            raise HTTPException(status_code=404, detail="unknown tag")
        return {"tag": tag, "rows": _to_json(service.list_items(tag, query))}

    @app.get("/api/items/{tag}/{tid}")
    async def item(tag: str, tid: str):
        t = service.item_detail(tag, tid)
        if t is None:
            raise HTTPException(status_code=404, detail="no such item")
        return _to_json(t)

    @app.get("/api/proposals")
    async def proposals():
        return _to_json(service.proposals_snapshot())

    @app.get("/api/status")
    async def status():
        return _to_json(service.status_snapshot())

    @app.get("/api/chat")
    async def chat():
        return {"turns": _to_json(service.chat_transcript())}

    @app.get("/api/architect")
    async def architect(name: str | None = None):
        return _to_json(service.architect_snapshot()
                        if name is None else
                        {"name": name,
                         "turns": service.architect_transcript(name)})

    @app.get("/api/tuples")
    async def tuples(tag: str | None = None):
        return _to_json(service.tuples_snapshot(tag))

    @app.get("/api/files")
    async def files():
        return _to_json(service.files_snapshot())

    @app.get("/api/help")
    async def help_snapshot():
        return _to_json(service.help_snapshot())

    # ── command dispatch ────────────────────────────────────────────────

    @app.post("/api/command")
    async def command(req: CommandRequest):
        ctx = CommandContext(tag=req.tag, tid=req.tid,
                             proposal_pid=req.proposal_pid)
        result = service.handle(req.line, ctx=ctx)
        job_ids = [runner.start(job) for job in result.jobs]
        return {
            "messages": _to_json(result.messages),
            "view": ({"name": result.view.name, "data": _to_json(result.view.data),
                      "scroll_end": result.view.scroll_end}
                     if result.view else None),
            "table": _to_json(result.table),
            "jobs": job_ids,
        }

    # ── job progress (SSE) ──────────────────────────────────────────────

    @app.get("/api/jobs/{jid}")
    async def job_status(jid: str):
        return runner.status(jid)

    @app.get("/api/jobs/{jid}/events")
    async def job_events(jid: str):
        queue = runner.get_queue(jid)
        if queue is None:
            raise HTTPException(status_code=404, detail="no such job")

        async def stream() -> AsyncIterator[str]:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(_to_json(event))}\n\n"
                if event.get("type") == "done":
                    break

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ── static browser shell ────────────────────────────────────────────

    if WEB_DIR.is_dir():
        @app.get("/")
        async def index():
            return FileResponse(WEB_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="ABoard — browser GUI")
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
    ap.add_argument("--host", default="127.0.0.1", help="bind host")
    ap.add_argument("--port", type=int, default=8000, help="bind port")
    args = ap.parse_args()

    if args.endpoint:
        if args.fresh or args.run_root:
            warnings.warn(
                "--fresh and --run-root are ignored with --endpoint (the "
                "server owns the board's persistence)", UserWarning,
                stacklevel=2)
        space = HttpSpace(args.endpoint)
    else:
        space, _ = open_space(args.run_root, fresh=args.fresh)
    board = Board(space, actor=args.name)

    mode = config.get(space, config.MODE, actor=args.name)
    chat_mode = mode if mode in ("human", "auto") else "human"
    stock_path = config.get(space, config.STOCK, actor=args.name)
    stock = load_stock(stock_path) if stock_path else None

    service = BoardService(
        board=board, human=args.name, chat_mode=chat_mode, stock=stock,
        llm=LLMConfig(model=args.model, base_url=args.base_url,
                      api_key=args.api_key))
    app = create_app(service)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
