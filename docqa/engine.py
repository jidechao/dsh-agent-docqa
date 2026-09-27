"""Document Q&A engine built on the DeepSeek Harness Python SDK.

Design in one paragraph: the SDK is a headless agent driver, not a chat app.
It owns the agent, its tools, its durable session, and its model calls; this
class owns the chat surface, the uploads, and the prompt assembly. Uploaded
documents become *workspace files* (``workspace/docs/*.md``) that the agent
reads with its own ``read`` / ``grep`` tools, so document size is bounded by the
compaction machinery rather than by the request that carries it.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .events import TurnEvent, TurnTranslator
from .ingest import DocEntry, Library, ingest_file

SYSTEM_RULES = """你是一个个人文档问答助手，工作在用户上传文档所形成的资料库中。

工作要求：
1. 只依据资料库（`docs/` 下的文档，以及随问题送来的图片）回答，不使用未经资料支持的外部知识。
2. 回答时标注来源：文件名 + 页号/章节/工作表名。
3. 文档中没有的信息，明确回答"文档中未提及"，不要编造或推测。
4. 需要细节时，用 read / grep / glob 工具到 `docs/` 下检索原文；图片直接看图像内容，不要凭记忆作答。
5. 用户问多个文件时，先分别定位，再给出对比和结论。"""

SMALL_LIBRARY_PROMPT = """## 文档清单

{manifest}

以上文档的全文已直接给出，通常无需再读文件。若你仍要用工具核对，
必须使用上表中的**确切 Markdown 路径**——文件名带内容哈希前缀，不要按原始文件名猜测。
「原文路径」是上传原件的相对路径（相对 agent 工作目录），可用 read / read_image 直接读取。

## 文档全文
{payload}"""

LARGE_LIBRARY_PROMPT = """## 文档清单

{manifest}

文档全文没有直接给出（总体量 {total:,} 字符）。请先用 grep 在 `docs/` 下定位相关片段，
再用 read 按上表中的确切路径读取上下文，然后回答。图片没有 Markdown 全文，
只能靠「原文路径」读取或使用随问题附上的图像块。"""

#: Appended when a turn carries inline raster images (or had to drop one).
IMAGE_NOTE_PROMPT = """## 随本轮问题送来的图片

{lines}

图片是**随问题直接送达的图像内容**，不是路径：不需要（也不应该）再用工具去 `docs/` 下找同名文件，
那里不会有图片的 Markdown。请直接看图回答，并在答案里说明依据的是哪张图；
清单里标「未附上」的图片这轮没有送达，不要猜测它的内容。"""

#: Raster formats the SDK wire protocol accepts as inline image blocks.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

#: The permission presets DSH's base profile ships. `workspace-write` is what a
#: fresh session pins; `danger-full-access` is the documented deployment override
#: (`DSH_PERMISSION_MODE`) and is the only mode that needs no sandbox runner.
PERMISSION_MODES = ("read-only", "workspace-write", "danger-full-access")


def resolve_permission_mode(*values: str | None) -> str | None:
    """First non-empty permission preset among ``values``, validated.

    ``None`` means "leave DSH's own default alone" (``workspace-write``). Both the
    docqa spelling (``DOCQA_PERMISSION_MODE``) and DSH's own (``DSH_PERMISSION_MODE``)
    are accepted so a `.env` written for either one works.
    """
    for value in values:
        if value and value.strip():
            mode = value.strip()
            if mode not in PERMISSION_MODES:
                raise ValueError(
                    f"未知的权限预设 {mode!r}；可选：{', '.join(PERMISSION_MODES)}"
                )
            return mode
    return None


@dataclass(slots=True)
class EngineConfig:
    library_root: Path
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"
    profile: str = "sdk"
    reasoning_effort: str | None = None
    max_tokens: int | None = 49_152
    #: Total characters of converted Markdown still cheap enough to inline.
    inline_budget: int = 60_000
    #: Inline raster images sent with a turn. Images have no Markdown twin, so
    #: the only way to answer from them is to send the bytes; these two caps keep
    #: a picture-heavy library from blowing up one request.
    max_images: int = 4
    max_image_bytes: int = 8 * 1024 * 1024
    #: Permission preset for the runtime's sessions (``read-only`` /
    #: ``workspace-write`` / ``danger-full-access``). ``None`` leaves DSH's own
    #: default (``workspace-write``) in place. See :data:`PERMISSION_MODES`.
    permission_mode: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    #: Alternative ``dsh`` executable; set it to run against a source checkout
    #: instead of the wheel-installed runtime.
    dsh_bin: str | None = None
    initialize_timeout_seconds: float = 120.0
    #: Extra cordis patch layers (``--patch``) layered over the profile. docqa mounts
    #: its token-stream forwarder (``docqa/plugin``) this way.
    patches: tuple[str, ...] = ()


class DocQAEngine:
    """Owns one SDK runtime, one document library, and one durable session."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.library: Library = Library(root=Path(config.library_root)).prepare()
        #: Identifies the SDK runtime this instance owns; a state file carrying a
        #: different token means a previous process owned the session.
        self._runtime_token = uuid.uuid4().hex
        self._state_runtime_token: str | None = None
        self._previous_sessions: list[str] = []
        self.session_id: str = self._load_state()
        self._harness = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- state

    @property
    def _state_file(self) -> Path:
        return self.library.root / "state.json"

    def _load_state(self) -> str:
        """Resolve the session id and document index, repairing from disk."""
        payload: dict = {}
        if self._state_file.exists():
            try:
                loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}

        docs = payload.get("docs")
        if isinstance(docs, list) and docs:
            self.library.docs = [DocEntry(**item) for item in docs if isinstance(item, dict)]
        else:
            # No usable index: the workspace itself is the source of truth.
            self.library.docs = _scan_workspace(self.library)

        existing = payload.get("session_id")
        if isinstance(existing, str) and existing:
            self.session_id = existing
            # May belong to another process; checked when a runtime actually starts.
            self._state_runtime_token = payload.get("runtime_token")
        else:
            # Nothing to conflict with, so this runtime can claim the fresh id now.
            self.session_id = f"docqa-{uuid.uuid4().hex[:12]}"
            self._state_runtime_token = self._runtime_token
            self._save_state()
        self._previous_sessions = [
            str(item) for item in payload.get("previous_session_ids", []) if isinstance(item, str)
        ][:5]
        return self.session_id

    def _save_state(self) -> None:
        self._state_file.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "runtime_token": self._state_runtime_token,
                    "previous_session_ids": self._previous_sessions,
                    "docs": [asdict(doc) for doc in self.library.docs],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _rotate_for_new_runtime(self) -> None:
        """Start a fresh conversation when a new SDK runtime owns this library.

        The SDK's JSON-RPC server only ever calls ``ctx.agents.create()``, which
        rejects a session id that already exists in the store; continuing a
        persisted session would need ``ctx.agents.resume()``, which the server
        does not expose (see ``packages/core/agent-loop/tests/resume.spec.ts``).
        So the durable JSONL log is a historical record, not something this
        transport can attach to — every runtime process starts a new session.
        """
        if self._state_runtime_token == self._runtime_token:
            return
        if self.session_id:
            self._previous_sessions = ([self.session_id] + self._previous_sessions)[:5]
        self.session_id = f"docqa-{uuid.uuid4().hex[:12]}"
        self._state_runtime_token = self._runtime_token
        self._save_state()

    def new_session(self) -> str:
        """Start a fresh conversation, keeping the ingested documents."""
        if self.session_id:
            self._previous_sessions = ([self.session_id] + self._previous_sessions)[:5]
        self.session_id = f"docqa-{uuid.uuid4().hex[:12]}"
        self._state_runtime_token = self._runtime_token
        self._save_state()
        return self.session_id

    # ------------------------------------------------------------- ingestion

    def add_document(self, path: Path | bytes, name: str | None = None):
        entry = ingest_file(self.library, path, name)
        self._save_state()
        return entry

    def add_documents(self, items: Sequence[tuple[str, bytes]]) -> list:
        return [self.add_document(payload, name) for name, payload in items]

    # ------------------------------------------------------------------ ask

    def _harness_or_start(self):
        if self._harness is None:
            from deepseek_harness import DeepSeekHarness

            # A brand-new runtime cannot adopt an existing session id.
            self._rotate_for_new_runtime()

            env: dict[str, str] = {}
            if self.config.api_key:
                env["DEEPSEEK_API_KEY"] = self.config.api_key
            if self.config.base_url:
                env["DEEPSEEK_BASE_URL"] = self.config.base_url
            if self.config.permission_mode:
                # DSH's documented deployment override: it changes the process-wide
                # sandbox fallback (and relaxes the approval policy to match).
                # `danger-full-access` is what makes the Windows shell/search tools
                # usable under the packaged runtime — there is no sandbox runner to
                # start, so nothing can fail to start. See README「Windows 沙箱 runner」.
                env["DSH_PERMISSION_MODE"] = self.config.permission_mode

            harness = DeepSeekHarness(
                provider=self.config.provider,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                max_tokens=self.config.max_tokens,
                cwd=str(self.library.workspace),
                dsh_home=str(self.library.home),
                profile=self.config.profile,
                dsh_bin=self.config.dsh_bin,
                patches=self.config.patches,
                env=env,
                initialize_timeout_seconds=self.config.initialize_timeout_seconds,
            )
            harness.start()
            self._harness = harness
        return self._harness

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None

    def __enter__(self) -> "DocQAEngine":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def library_images(self) -> list[Path]:
        """Image documents in the library, newest first.

        An uploaded image has no Markdown twin (there is no OCR in the chain), so
        the only way to answer from it is to send the bytes. The web upload path
        ingests images as ordinary documents, which makes the library itself the
        attachment queue: whatever the user uploaded is what they want to ask
        about, so it rides along with the question instead of waiting for a tool
        call that may not even be able to run.
        """
        found: list[Path] = []
        for entry in reversed(self.library.docs):
            if entry.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            path = self.library.workspace / entry.upload_path
            if path.is_file():
                found.append(path)
        return found

    def _select_images(self, images: Sequence[Path]) -> tuple[list[Path], list[str]]:
        """Pick the images to inline: explicit attachments first, then the library.

        Returns the chosen paths plus human-readable reasons for anything that was
        dropped, so the prompt can tell the model a picture was *not* delivered
        rather than letting it invent content.
        """
        chosen: list[Path] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for candidate in [*(Path(item) for item in images), *self.library_images()]:
            key = str(candidate.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            name = candidate.name
            if len(chosen) >= max(self.config.max_images, 0):
                skipped.append(f"{name}（超过每轮 {self.config.max_images} 张上限）")
                continue
            if (mimetypes.guess_type(name)[0] or "") not in _IMAGE_MIMES:
                skipped.append(f"{name}（不是 SDK 支持的栅格格式 png/jpeg/webp/gif）")
                continue
            try:
                size = candidate.stat().st_size
            except OSError as error:
                skipped.append(f"{name}（读不到文件：{type(error).__name__}）")
                continue
            if size > self.config.max_image_bytes:
                skipped.append(
                    f"{name}（{size / 1048576:.1f} MB，超过 "
                    f"{self.config.max_image_bytes / 1048576:.0f} MB 上限）"
                )
                continue
            chosen.append(candidate)
        return chosen, skipped

    def _build_input(self, question: str, images: Sequence[Path] = ()) -> list[dict]:
        total = self.library.total_chars()
        manifest = self.library.manifest()
        if total and total <= self.config.inline_budget:
            body = SMALL_LIBRARY_PROMPT.format(manifest=manifest, payload=self.library.inline_payload())
        else:
            body = LARGE_LIBRARY_PROMPT.format(manifest=manifest, total=total)

        attached, skipped = self._select_images(images)
        if attached or skipped:
            lines = [f"- 第 {index} 张：`{path.name}`" for index, path in enumerate(attached, start=1)]
            lines += [f"- 未附上：{reason}" for reason in skipped]
            body = f"{body}\n\n{IMAGE_NOTE_PROMPT.format(lines=chr(10).join(lines))}"

        prompt = f"{SYSTEM_RULES}\n\n{body}\n\n## 用户问题\n\n{question}\n"
        blocks: list[dict] = [{"type": "text", "text": prompt}]

        for image in attached:
            mime = mimetypes.guess_type(image.name)[0] or "image/png"
            blocks.append({
                "type": "image",
                "data": base64.b64encode(image.read_bytes()).decode("ascii"),
                "mimeType": mime,
            })
        return blocks

    def ask(self, question: str, images: Sequence[Path] = (),
            on_event: Callable[[TurnEvent], None] | None = None):
        """Run one blocking turn and return the SDK ``RunResult``.

        ``on_event`` is invoked from the SDK's notification thread as each
        durable fact lands, so it must not block.
        """
        content = self._build_input(question, images)
        with self._lock:
            harness = self._harness_or_start()
            translator = TurnTranslator(self.session_id)

            def relay(notification) -> None:
                if on_event is None:
                    return
                for event in translator.feed(notification):
                    on_event(event)

            try:
                return harness.run(content, session_id=self.session_id, on_notification=relay)
            except Exception as error:
                # Last-resort self-heal: a session id persisted by another runtime
                # is unrecoverable through this transport, so start a new one and
                # retry once rather than failing the user's question.
                if "already exists" not in str(error):
                    raise
                self._previous_sessions = ([self.session_id] + self._previous_sessions)[:5]
                self.session_id = f"docqa-{uuid.uuid4().hex[:12]}"
                self._state_runtime_token = self._runtime_token
                self._save_state()
                translator = TurnTranslator(self.session_id)
                if on_event is not None:
                    on_event(TurnEvent("status", {"status": "running", "note": "会话已重建"}))
                return harness.run(content, session_id=self.session_id, on_notification=relay)

    def ask_stream(self, question: str, images: Sequence[Path] = ()) -> Iterator[TurnEvent]:
        """Run one turn on a worker thread, yielding events until it settles.

        Terminal events are ``done`` (with the final answer) or ``error``. See
        :mod:`docqa.events` for exactly which facts are live and which arrive
        once their step commits.
        """
        outbox: queue.Queue[TurnEvent | None] = queue.Queue()
        started = time.monotonic()

        # Say up front what rode along: images are the one part of the corpus the
        # model cannot reach through a file path, so the UI should show that the
        # visual channel is in play (and when it had to drop a picture).
        attached, skipped = self._select_images(images)
        if attached or skipped:
            note = f"已附上 {len(attached)} 张图片"
            if skipped:
                note += f"，跳过 {len(skipped)} 张"
            outbox.put(TurnEvent("status", {"status": "running", "note": note}))

        def worker() -> None:
            try:
                result = self.ask(question, images, on_event=outbox.put)
                elapsed = int((time.monotonic() - started) * 1000)
                answer = result.final_response or ""
                # 轮次没跑完又没有产出：这是失败，不是"完成"——把它当成功会让界面
                # 显示一个绿色徽章和空答案，用户完全不知道发生了什么（配错
                # DEEPSEEK_BASE_URL / 凭据无效 / 上下文超限都会走到这里）。
                if not answer.strip() and result.finish_reason not in (None, "completed"):
                    outbox.put(TurnEvent("error", {
                        "message": (
                            f"本轮以 {result.finish_reason} 结束，且没有产出任何回答。"
                            "常见原因：模型接口不可达（检查 DEEPSEEK_BASE_URL）、凭据无效或过期、"
                            "单轮上下文超限。"
                        ),
                        "finish_reason": result.finish_reason,
                        "elapsed_ms": elapsed,
                    }))
                else:
                    outbox.put(TurnEvent("done", {
                        "answer": answer,
                        "finish_reason": result.finish_reason,
                        "session_id": result.session_id,
                        "elapsed_ms": elapsed,
                    }))
            except Exception as error:  # surfaced to the UI, never swallowed
                outbox.put(TurnEvent("error", {
                    "message": f"{type(error).__name__}: {error}",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }))
            finally:
                outbox.put(None)

        threading.Thread(target=worker, name="docqa-turn", daemon=True).start()
        while True:
            item = outbox.get()
            if item is None:
                return
            yield item


def _scan_workspace(library: Library) -> list[DocEntry]:
    """Rebuild the document list from ``workspace/docs`` when state is absent."""
    entries: list[DocEntry] = []
    for markdown in sorted(library.markdown.glob("*.md")):
        slug = markdown.stem
        originals = sorted(library.uploads.glob(f"{slug}*"))
        original = originals[0] if originals else None
        entries.append(DocEntry(
            slug=slug,
            source_name=original.name.split("-", 1)[-1] if original else slug,
            suffix=Path(original.name).suffix.lower() if original else "",
            bytes=original.stat().st_size if original else 0,
            upload_path=f"uploads/{original.name}" if original else "",
            markdown_path=f"docs/{markdown.name}",
            chars=len(markdown.read_text(encoding="utf-8", errors="replace")),
        ))
    return entries
