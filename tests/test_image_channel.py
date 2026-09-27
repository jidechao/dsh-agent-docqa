"""Regression test for the image channel — no model call, no dsh runtime.

Run with the project venv::

    .venv\\Scripts\\python.exe tests\\test_image_channel.py

Why this test exists: images are the one part of the corpus that cannot reach the
model as a file path. An uploaded PNG has no Markdown twin, so if the host forgets
to inline it the agent can only guess at paths — which is exactly the bug this
covers (`/api/chat` used to pass the question and nothing else, so a picture-only
library answered "读不到文档"). Everything here is offline: a throwaway library in
a temp directory, a 1x1 PNG, and a stub engine for the HTTP layer.
"""

from __future__ import annotations

import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from docqa.engine import DocQAEngine, EngineConfig  # noqa: E402
from docqa.ingest import IMAGE_SUFFIXES, DocEntry, Library  # noqa: E402
from docqa.server import create_app, resolve_images  # noqa: E402

#: Smallest possible valid PNG (1x1, transparent).
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, label: str, condition: bool, detail: str = "") -> None:
        print(f"{'PASS' if condition else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not condition:
            self.failures.append(label)


def make_library(root: Path, *, suffix: str = ".png") -> tuple[Library, Path, DocEntry]:
    """A throwaway library holding one registered image document."""
    library = Library(root=root).prepare()
    name = f"deadbeef-chart{suffix}"
    path = library.uploads / name
    path.write_bytes(PNG)
    entry = DocEntry(
        slug=name, source_name=f"chart{suffix}", suffix=suffix, bytes=len(PNG),
        upload_path=f"uploads/{name}", markdown_path=None, chars=0,
        engine="image", note="图片，随问题走视觉通道（未装 tesseract，不做 OCR）",
    )
    library.docs = [entry]
    library.write_index()
    return library, path, entry


class StubEngine:
    """Stands in for DocQAEngine so the HTTP layer never starts a real runtime."""

    def __init__(self, library: Library) -> None:
        self.library = library
        self.session_id = "stub-session"
        self.config = type("Cfg", (), {"model": "stub", "profile": "sdk", "inline_budget": 60000})()
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def add_document(self, payload: bytes, name: str):  # noqa: ANN201
        entry = DocEntry(
            slug=name, source_name=name, suffix=Path(name).suffix.lower(), bytes=len(payload),
            upload_path=f"uploads/{name}", markdown_path=None, chars=0,
            engine="image" if Path(name).suffix.lower() in IMAGE_SUFFIXES else "text",
            note="图片，随问题走视觉通道",
        )
        (self.library.uploads / name).write_bytes(bytes(payload))
        self.library.docs.append(entry)
        return entry

    def ask_stream(self, question: str, images=()):  # noqa: ANN001, ANN202
        self.calls.append((question, tuple(Path(p).name for p in images)))
        return iter(())

    def new_session(self) -> str:
        return self.session_id


def main() -> int:
    check = Checker()

    with tempfile.TemporaryDirectory(prefix="docqa-image-test-") as scratch:
        root = Path(scratch) / "library"
        library, image_path, entry = make_library(root)
        slug = image_path.name

        # ---------------------------------------------------------- engine side
        # A fresh engine rebuilds its index from state.json / the workspace, so the
        # registered entry is re-attached here (a real ingest persists it instead).
        def new_engine(**kwargs) -> DocQAEngine:
            built = DocQAEngine(EngineConfig(library_root=root, **kwargs))
            built.library.docs = [entry]
            return built

        engine = new_engine()
        check("library_images() finds the uploaded image", [p.name for p in engine.library_images()] == [slug])

        blocks = engine._build_input("图中描述了啥？")
        kinds = [block["type"] for block in blocks]
        check("prompt block comes first", kinds[0] == "text", str(kinds))
        check("image is inlined with the question", kinds == ["text", "image"], str(kinds))
        check("image block is base64 of the original bytes",
              base64.b64decode(blocks[1]["data"]) == PNG)
        check("image block carries the media type", blocks[1]["mimeType"] == "image/png", blocks[1]["mimeType"])

        prompt = blocks[0]["text"]
        check("prompt announces the attached image", "随本轮问题送来的图片" in prompt and slug in prompt)
        check("prompt keeps the model out of docs/ for images", "不会有图片的 Markdown" in prompt)
        check("manifest exposes the original path", "原文路径" in prompt and f"uploads/{slug}" in prompt)
        check("index.md gets the original path too", f"uploads/{slug}" in library.index_file.read_text(encoding="utf-8"))

        capped = new_engine(max_images=0)._build_input("看图")
        check("max_images=0 attaches nothing", [b["type"] for b in capped] == ["text"])
        check("max_images=0 says why", "未附上" in capped[0]["text"])

        tiny = new_engine(max_image_bytes=1)._build_input("看图")
        check("oversized image is skipped", [b["type"] for b in tiny] == ["text"])
        check("oversized image says why", "超过" in tiny[0]["text"])

        # ------------------------------------------------------- slug resolution
        check("registered slug resolves", [p.name for p in resolve_images(engine, [slug])] == [slug])
        check("unknown slug is dropped", resolve_images(engine, ["nope.png"]) == [])
        check("path-shaped slug is dropped", resolve_images(engine, [f"../../{slug}"]) == [])
        check("windows path-shaped slug is dropped", resolve_images(engine, [f"..\\..\\{slug}"]) == [])
        check("non-image slug is dropped", resolve_images(engine, ["notes.md"]) == [])
        check("non-list payload is dropped", resolve_images(engine, "chart.png") == [])

        # ----------------------------------------------------------- HTTP layer
        stub = StubEngine(library)
        client = TestClient(create_app(stub))  # type: ignore[arg-type]

        upload = client.post("/api/documents", files={"files": ("probe.png", PNG, "image/png")})
        check("POST /api/documents succeeds", upload.status_code == 200, upload.text[:120])
        reported = upload.json()["documents"][0]
        check("upload reports image=true", reported.get("image") is True, str(reported))
        check("upload reports the slug the browser must send back", reported.get("slug") == "probe.png", str(reported))
        check("upload reports the stored original path",
              str(reported.get("upload", "")).startswith("uploads/"), str(reported.get("upload")))

        chat = client.post("/api/chat", json={"question": "图中描述了啥？", "images": [slug]})
        check("POST /api/chat accepts the image selection", chat.status_code == 202, chat.text[:160])
        body = chat.json()
        check("chat response counts the attached images", body.get("images") == 1, str(body))
        for _ in range(200):
            if client.get(f"/api/chat/{body['job_id']}").json()["finished"]:
                break
            time.sleep(0.02)
        check("job snapshot exposes the count",
              client.get(f"/api/chat/{body['job_id']}").json().get("images") == 1)
        check("engine.ask_stream receives the resolved image",
              bool(stub.calls) and stub.calls[0][1] == (slug,), str(stub.calls))

        hostile = client.post("/api/chat", json={"question": "再看", "images": ["../../etc/passwd", 7]})
        check("hostile selection still starts a turn", hostile.status_code == 202, hostile.text[:160])
        check("hostile selection attaches nothing explicit", hostile.json().get("images") == 0, hostile.text[:160])

        # A library whose only document is text must not grow an image section.
        text_root = Path(scratch) / "text-only"
        text_library, text_image, _ = make_library(text_root)
        text_image.unlink()
        text_library.docs = []
        text_engine = DocQAEngine(EngineConfig(library_root=text_root))
        text_engine.library.docs = []
        text_blocks = text_engine._build_input("没有图片时也要正常")
        check("no images means no image section", [b["type"] for b in text_blocks] == ["text"])

    print()
    if check.failures:
        print(f"{len(check.failures)} FAILED: {check.failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
