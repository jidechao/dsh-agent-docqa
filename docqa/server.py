"""REST + SSE API in front of the docqa engine.

Transport
---------
``POST /api/chat`` starts a turn and returns a ``job_id`` immediately.
``GET /api/chat/{job_id}/events`` is a Server-Sent Events stream carrying every
:class:`~docqa.events.TurnEvent` for that turn, in order, as it happens.

SSE rather than WebSocket on purpose: the traffic is one-directional
(server → browser), it survives proxies as plain HTTP, it reconnects on its own
with ``Last-Event-ID``, and a dropped turn does not need a handshake to resume —
the job keeps its event buffer until it is evicted.

Concurrency
-----------
The SDK runtime is not re-entrant here: the engine serialises turns with a lock,
so a second ``POST /api/chat`` while one is running returns **409** instead of
queueing silently. The start of a turn is therefore explicit in the API.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .engine import DocQAEngine, EngineConfig, resolve_permission_mode
from .events import TurnEvent
from .ingest import IMAGE_SUFFIXES, engine_availability

STATIC_DIR = Path(__file__).resolve().parent / "static"
JOB_RETENTION_SECONDS = 900
HEARTBEAT_SECONDS = 15.0


def resolve_images(engine: DocQAEngine, values: object) -> list[Path]:
    """Turn client-supplied slugs into library image paths.

    Accepts only the *bare file name* of an image already registered in the
    library: a request can select an attachment, never point the engine at an
    arbitrary file. Anything path-shaped, unregistered, or non-image is dropped
    silently — the engine still attaches the library's own images automatically.
    """
    if not isinstance(values, list):
        return []
    known = {entry.slug: entry for entry in engine.library.docs}
    resolved: list[Path] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        if raw != Path(raw).name:  # separators, "..", drive letters: not a slug
            continue
        entry = known.get(raw)
        if entry is None or entry.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        path = engine.library.uploads / raw
        if path.is_file():
            resolved.append(path)
    return resolved


@dataclass
class Listener:
    """One SSE reader: its queue plus the loop a worker thread must post to."""

    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop

    def push(self, item) -> None:
        if self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)
        except RuntimeError:
            pass


@dataclass
class TurnJob:
    """One in-flight or finished turn plus its replayable event buffer."""

    id: str
    question: str
    #: Library-resolved image paths to inline with this turn's prompt.
    images: tuple[Path, ...] = ()
    created: float = field(default_factory=time.time)
    events: list[TurnEvent] = field(default_factory=list)
    finished: bool = False
    error: str | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "images": len(self.images),
            "finished": self.finished,
            "error": self.error,
            "event_count": len(self.events),
        }


class JobRegistry:
    """Turns indexed by id, with a thread-safe fan-out to SSE readers."""

    def __init__(self, engine: DocQAEngine) -> None:
        self.engine = engine
        self.jobs: dict[str, TurnJob] = {}
        self._active: str | None = None
        self._lock = threading.Lock()
        self._listeners: dict[str, list[Listener]] = {}

    # ---------------------------------------------------------------- start

    def start(self, question: str, images: Sequence[Path] = ()) -> TurnJob:
        with self._lock:
            running = self.jobs.get(self._active) if self._active else None
            if running is not None and not running.finished:
                raise RuntimeError("BUSY")
            job = TurnJob(id=uuid.uuid4().hex[:16], question=question, images=tuple(images))
            self.jobs[job.id] = job
            self._listeners[job.id] = []
            self._active = job.id
            self._evict_old()
        threading.Thread(target=self._run, args=(job,), name=f"turn-{job.id}", daemon=True).start()
        return job

    def _run(self, job: TurnJob) -> None:
        try:
            for event in self.engine.ask_stream(job.question, job.images):
                self._publish(job, event)
        except Exception as error:  # ask_stream traps its own errors; this is belt-and-braces
            job.error = f"{type(error).__name__}: {error}"
            self._publish(job, TurnEvent("error", {"message": job.error}))
        finally:
            job.finished = True
            for listener in list(self._listeners.get(job.id, [])):
                listener.push(None)

    def _publish(self, job: TurnJob, event: TurnEvent) -> None:
        event.seq = len(job.events) + 1
        job.events.append(event)
        for listener in list(self._listeners.get(job.id, [])):
            listener.push(event)

    def push_deltas(self, session_id: str, deltas: list) -> int:
        """Fan runtime-side token deltas out to the live readers of the active job.

        Deltas are *transient*: they are not appended to ``job.events`` (the
        replayable log) because the authoritative text is the committed
        ``text`` / ``done`` event that arrives when the step commits. Keeping
        them out of the replay buffer also keeps reconnect snapshots cheap.
        """
        with self._lock:
            job = self.jobs.get(self._active) if self._active else None
        if job is None or job.finished:
            return 0
        accepted = 0
        listeners = list(self._listeners.get(job.id, []))
        for item in deltas:
            if not isinstance(item, dict):
                continue
            if item.get("sessionId") and item["sessionId"] != session_id:
                continue
            kind = item.get("kind")
            text = item.get("text")
            if kind not in ("text", "reasoning") or not isinstance(text, str) or not text:
                continue
            event = TurnEvent("delta", {"kind": kind, "text": text, "index": item.get("index")})
            for listener in listeners:
                listener.push(event)
            accepted += 1
        return accepted

    # --------------------------------------------------------------- stream

    def subscribe(self, job_id: str, after: int) -> tuple[list[TurnEvent], Listener]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        listener = Listener(queue=asyncio.Queue(), loop=asyncio.get_running_loop())
        self._listeners.setdefault(job_id, []).append(listener)
        backlog = [event for event in job.events if event.seq > after]
        if job.finished:
            listener.push(None)
        return backlog, listener

    def unsubscribe(self, job_id: str, listener: Listener) -> None:
        listeners = self._listeners.get(job_id)
        if listeners and listener in listeners:
            listeners.remove(listener)

    def _evict_old(self) -> None:
        cutoff = time.time() - JOB_RETENTION_SECONDS
        for job_id, job in list(self.jobs.items()):
            if job.finished and job.created < cutoff:
                self.jobs.pop(job_id, None)
                self._listeners.pop(job_id, None)


def create_app(engine: DocQAEngine) -> FastAPI:
    app = FastAPI(title="docqa", version="1.0")
    registry = JobRegistry(engine)
    #: Shared secret for the runtime-side stream forwarder (set by run_web.py).
    stream_token = os.environ.get("DOCQA_STREAM_TOKEN", "")

    # ------------------------------------------------------------------ state

    @app.get("/api/state")
    def state() -> dict:
        return {
            "session_id": engine.session_id,
            "library": str(engine.library.root),
            "model": engine.config.model,
            "profile": engine.config.profile,
            "inline_budget": engine.config.inline_budget,
            "total_chars": engine.library.total_chars(),
            "documents": [
                {
                    "slug": entry.slug,
                    "name": entry.source_name,
                    "suffix": entry.suffix,
                    "bytes": entry.bytes,
                    "markdown": entry.markdown_path,
                    "chars": entry.chars,
                    "pages": entry.pages,
                    "engine": entry.engine,
                    "note": entry.note,
                }
                for entry in engine.library.docs
            ],
        }

    @app.get("/api/engines")
    def engines() -> dict:
        return engine_availability()

    # -------------------------------------------------------------- documents

    @app.post("/api/documents")
    async def upload(files: list[UploadFile] = File(...)) -> dict:
        if not files:
            raise HTTPException(status_code=400, detail="没有收到文件")
        results = []
        for upload_file in files:
            payload = await upload_file.read()
            name = upload_file.filename or "upload.bin"
            # Conversion shells out (LibreOffice) and parses; keep the loop free.
            entry = await asyncio.to_thread(engine.add_document, payload, name)
            results.append({
                "name": entry.source_name,
                "slug": entry.slug,
                "upload": entry.upload_path,
                "image": entry.suffix.lower() in IMAGE_SUFFIXES,
                "markdown": entry.markdown_path,
                "chars": entry.chars,
                "pages": entry.pages,
                "engine": entry.engine,
                "note": entry.note,
                "ok": entry.markdown_path is not None,
            })
        return {"documents": results, "state": state()}

    @app.delete("/api/documents")
    def clear_documents() -> dict:
        from .ingest import reset_library

        reset_library(engine.library)
        return {"ok": True, "state": state()}

    @app.post("/api/session")
    def new_session() -> dict:
        return {"session_id": engine.new_session(), "state": state()}

    # ------------------------------------------------------------------- chat

    @app.post("/api/chat")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")
        if not engine.library.docs:
            raise HTTPException(status_code=409, detail="资料库为空，请先上传文档")
        # Attachments are optional: an explicit list selects library images for
        # this turn, and the engine additionally inlines the library's own images.
        images = resolve_images(engine, body.get("images"))
        try:
            job = registry.start(question, images)
        except RuntimeError:
            raise HTTPException(status_code=409, detail="上一轮还在执行，请等它结束")
        return JSONResponse(
            {"job_id": job.id, "session_id": engine.session_id, "images": len(images)},
            status_code=202,
        )

    @app.get("/api/chat/{job_id}")
    def job_status(job_id: str) -> dict:
        job = registry.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未知的 job")
        return job.snapshot()

    @app.get("/api/chat/{job_id}/events")
    async def job_events(job_id: str, request: Request, last_event_id: str | None = None) -> StreamingResponse:
        after = 0
        if last_event_id:
            try:
                after = int(last_event_id)
            except ValueError:
                after = 0
        # EventSource sends Last-Event-ID as a header on reconnect.
        header_id = request.headers.get("last-event-id")
        if header_id and not last_event_id:
            try:
                after = int(header_id)
            except ValueError:
                after = 0
        try:
            backlog, listener = registry.subscribe(job_id, after)
        except KeyError:
            raise HTTPException(status_code=404, detail="未知的 job")

        async def stream():
            try:
                for event in backlog:
                    yield event.to_sse()
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(listener.queue.get(), timeout=HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        yield "event: end\ndata: {}\n\n"
                        return
                    yield item.to_sse()
            finally:
                registry.unsubscribe(job_id, listener)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------- runtime stream hook

    @app.post("/api/agent-stream")
    async def agent_stream(request: Request) -> dict:
        """Token-level deltas pushed by the runtime-side forwarder plugin.

        Not a public API: it exists only for ``docqa/plugin`` running *inside* the
        SDK runtime, which is the only place that can see ``agent/assistant-stream``.
        Guarded by the per-process token because anything on this machine could
        otherwise POST arbitrary text into someone's answer.
        """
        if stream_token and request.headers.get("x-docqa-token") != stream_token:
            raise HTTPException(status_code=403, detail="bad stream token")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="body 不是 JSON")
        deltas = body.get("deltas") if isinstance(body, dict) else None
        if not isinstance(deltas, list):
            raise HTTPException(status_code=400, detail="deltas 必须是数组")
        return {"ok": True, "accepted": registry.push_deltas(engine.session_id, deltas)}

    # --------------------------------------------------------- genui actions

    @app.post("/api/genui-action")
    async def genui_action(request: Request) -> JSONResponse:
        """Turn one GenUI component action into a normal agent turn.

        This is the docqa equivalent of the genui plugin's ``[genui-action]``
        loop: a click in a rendered card becomes the next user message, so the
        model can answer it with an updated ```dsh-ui fence. It reuses the exact
        same job/SSE machinery as ``/api/chat``.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="body 不是 JSON")
        action = str((body or {}).get("action") or "").strip()
        if not action:
            raise HTTPException(status_code=400, detail="action 不能为空")
        payload = (body or {}).get("payload")
        data = ""
        if isinstance(payload, (dict, list)) and payload:
            data = " 组件数据: " + json.dumps(payload, ensure_ascii=False)
        question = (
            f'[genui-action] {action}。用户刚刚在界面中触发了动作 "{action}"，'
            f"请根据组件数据执行相应操作，并用 ```dsh-ui 围栏输出更新后的界面。{data}"
        )
        try:
            job = registry.start(question)
        except RuntimeError:
            raise HTTPException(status_code=409, detail="上一轮还在执行，请等它结束")
        return JSONResponse({"job_id": job.id, "session_id": engine.session_id}, status_code=202)

    # ------------------------------------------------------------------ static

    @app.get("/plugin-assets/{name}")
    def plugin_asset(name: str) -> FileResponse:
        """Serve the genui renderer assets (mermaid.js / three.js).

        They belong to the installed dsh-genui package; docqa only serves them so
        the browser renderer can lazy-load them on demand instead of shipping a
        second copy of 4 MB of vendor code.
        """
        assets = Path(os.environ.get("DOCQA_GENUI_ASSETS", ""))
        target = (assets / name).resolve()
        if not assets or not target.is_file() or assets.resolve() not in target.parents:
            raise HTTPException(status_code=404, detail="asset 不存在")
        return FileResponse(target)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "documents": len(engine.library.docs), "session_id": engine.session_id}

    return app


def default_engine(library: str | None = None, model: str | None = None) -> DocQAEngine:
    """Build the engine the way ``run_web.py`` does, from the environment."""
    root = Path(library or os.environ.get("DOCQA_LIBRARY", ".docqa")).expanduser().resolve()
    dsh_bin = os.environ.get("DOCQA_DSH_BIN")
    patches = tuple(
        item for item in os.environ.get("DOCQA_PATCHES", "").split(os.pathsep) if item
    )
    return DocQAEngine(EngineConfig(
        library_root=root,
        model=model or os.environ.get("DSH_MODEL", "deepseek-flash"),
        profile=os.environ.get("DOCQA_PROFILE", "sdk"),
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL"),
        dsh_bin=dsh_bin,
        patches=patches,
        inline_budget=int(os.environ.get("DOCQA_INLINE_BUDGET", "60000")),
        max_images=int(os.environ.get("DOCQA_MAX_IMAGES", "4")),
        max_image_bytes=int(os.environ.get("DOCQA_IMAGE_MAX_BYTES", str(8 * 1024 * 1024))),
        permission_mode=resolve_permission_mode(
            os.environ.get("DOCQA_PERMISSION_MODE"), os.environ.get("DSH_PERMISSION_MODE")
        ),
        initialize_timeout_seconds=float(os.environ.get("DOCQA_INIT_TIMEOUT", "180")),
    ))


__all__ = ["create_app", "default_engine", "resolve_images", "JobRegistry", "TurnJob", "Listener"]
