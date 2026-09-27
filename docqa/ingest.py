"""Host-side document ingestion: verbatim originals + Markdown conversions.

The Python SDK cannot put a PDF into the agent's context the way the Web UI can:
the SDK wire protocol only accepts text blocks and inline raster images
(``SdkEncodedImageBlock``). Anything else has to reach the agent as a *file in
its workspace*. So this module owns the conversion step, and the agent owns the
reading step (``read`` / ``grep`` over ``workspace/docs``).

Conversion is a three-tier chain, tried in order per file:

1. **native** — a structure-preserving pure-Python parser (pypdf, python-docx,
   openpyxl, xlrd, python-pptx, csv, striprtf). Fast, no subprocess, keeps
   headings and table row/column relationships.
2. **MarkItDown** — Microsoft's converter, ~20 formats, thin structure but very
   broad coverage (epub, msg, ipynb, html, zip, ...).
3. **LibreOffice headless** — the universal backstop. Legacy and ODF formats
   (``.doc``/``.xls``/``.ppt``/``.odt``/``.ods``/``.odp``/WPS) are converted to
   their modern equivalent and re-enter tier 1, so they get real structure
   rather than a flattened text dump.

The real format is sniffed from magic bytes, not trusted from the extension, so
a ``.doc`` renamed to ``.docx`` still converts correctly.

Layout produced under one library root::

    <library>/workspace/uploads/<slug>   verbatim original bytes
    <library>/workspace/docs/<slug>.md   Markdown conversion
    <library>/workspace/INDEX.md         manifest the agent may read
    <library>/.cache/                    LibreOffice profile, scratch space
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".sql", ".sh",
}

#: Suffixes copied through verbatim. Markup and tabular formats are excluded so
#: they reach a real converter instead of arriving as raw tags.
PASSTHROUGH_SUFFIXES = TEXT_SUFFIXES - {".html", ".htm", ".xml", ".csv", ".tsv"}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

#: Legacy / ODF formats LibreOffice rewrites into a modern container.
LIBREOFFICE_TARGETS = {
    ".doc": ".docx", ".dot": ".docx", ".wps": ".docx", ".odt": ".docx", ".ott": ".docx",
    ".xls": ".xlsx", ".et": ".xlsx", ".ods": ".xlsx", ".ots": ".xlsx",
    ".ppt": ".pptx", ".dps": ".pptx", ".odp": ".pptx", ".otp": ".pptx",
    ".rtf": ".docx", ".docm": ".docx",
}

MAX_SHEET_ROWS = 200
MAX_TABLE_COLS = 24
CONVERT_TIMEOUT_SECONDS = 180


@dataclass(slots=True)
class DocEntry:
    """One ingested document, as recorded in the workspace manifest."""

    slug: str
    source_name: str
    suffix: str
    bytes: int
    upload_path: str
    markdown_path: str | None
    chars: int
    pages: int | None = None
    note: str = ""
    ocr_suspect: bool = False
    engine: str = ""

    def manifest_line(self) -> str:
        parts = [f"- `{self.source_name}` → `{self.markdown_path}`"]
        if self.pages is not None:
            parts.append(f"{self.pages} 页")
        parts.append(f"{self.chars:,} 字符")
        if self.engine:
            parts.append(f"[{self.engine}]")
        if self.note:
            parts.append(self.note)
        return " | ".join(parts)


@dataclass(slots=True)
class Conversion:
    """One converter's output."""

    text: str
    engine: str
    pages: int | None = None
    note: str = ""
    ocr_suspect: bool = False


@dataclass(slots=True)
class ConversionContext:
    """Writable scratch space shared by the converter chain."""

    cache: Path

    def prepare(self) -> "ConversionContext":
        self.cache.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(slots=True)
class Library:
    """A document library: its workspace, its DSH home, and its doc index."""

    root: Path
    docs: list[DocEntry] = field(default_factory=list)

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def cache(self) -> Path:
        return self.root / ".cache"

    @property
    def uploads(self) -> Path:
        return self.workspace / "uploads"

    @property
    def markdown(self) -> Path:
        return self.workspace / "docs"

    @property
    def index_file(self) -> Path:
        return self.workspace / "INDEX.md"

    def prepare(self) -> "Library":
        for directory in (self.workspace, self.uploads, self.markdown, self.home, self.cache):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def context(self) -> ConversionContext:
        return ConversionContext(cache=self.cache).prepare()

    def total_chars(self) -> int:
        return sum(entry.chars for entry in self.docs)

    def manifest(self) -> str:
        """The document table, shared by INDEX.md and the prompt.

        The original-path column matters most for images: they have no Markdown
        twin, so `read_image` on that path is the only way to reach them from a
        tool (the prompt normally ships them inline as image blocks).
        """
        lines = [
            "| 原始文件 | Markdown 路径 | 原文路径 | 页数/备注 |",
            "| --- | --- | --- | --- |",
        ]
        for entry in self.docs:
            note = " ".join(part for part in (f"{entry.pages} 页" if entry.pages else "", entry.note) if part)
            original = f"`{entry.upload_path}`" if entry.upload_path else "-"
            lines.append(
                f"| `{entry.source_name}` | `{entry.markdown_path or '-'}` | {original} | {note.strip() or '-'} |"
            )
        return "\n".join(lines)

    def write_index(self) -> None:
        lines = [
            "# 已上传文档索引",
            "",
            f"共 {len(self.docs)} 个文件，Markdown 合计约 {self.total_chars():,} 字符。",
            "",
            "Markdown 文件名带内容哈希前缀，**请按上表的确切路径读取**，不要按原始文件名猜测。",
            "图片没有 Markdown 全文：它们会作为图像块随问题一起送达，也可以按「原文路径」用 read_image 读。",
            "",
            self.manifest(),
        ]
        self.index_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def inline_payload(self) -> str:
        """Concatenated Markdown for small libraries that fit the context budget."""
        chunks: list[str] = []
        for entry in self.docs:
            if entry.markdown_path is None:
                continue
            text = (self.workspace / entry.markdown_path).read_text(encoding="utf-8", errors="replace")
            chunks.append(f"\n\n===== 文件：{entry.source_name} =====\n\n{text}")
        return "".join(chunks)


# --------------------------------------------------------------- small helpers

def _slug(name: str, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()[:8]
    stem = Path(name).stem
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-") or "file"
    suffix = re.sub(r"[^0-9A-Za-z.]+", "", Path(name).suffix)
    return f"{digest}-{safe[:60]}{suffix}"


def _table(rows: list[list[object]]) -> str:
    if not rows:
        return "_(空表)_"
    width = max(len(row) for row in rows)
    out: list[str] = []
    for index, row in enumerate(rows):
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") if cell is not None else "" for cell in row]
        cells += [""] * (width - len(cells))
        out.append("| " + " | ".join(cells) + " |")
        if index == 0:
            out.append("| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(out)


def _is_probably_text(payload: bytes) -> bool:
    if b"\x00" in payload[:8000]:
        return False
    try:
        payload[:8000].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# ------------------------------------------------------------ format sniffing

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff(path: Path, declared_suffix: str) -> str:
    """Return the canonical format key, trusting bytes over the extension."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return declared_suffix

    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(OLE_SIGNATURE):
        return _sniff_ole(path, declared_suffix)
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path, declared_suffix)
    return declared_suffix


def _sniff_ole(path: Path, declared_suffix: str) -> str:
    try:
        import olefile

        if not olefile.isOleFile(str(path)):
            return declared_suffix
        with olefile.OleFileIO(str(path)) as ole:
            names = {entry[0] for entry in ole.listdir()}
    except Exception:
        return declared_suffix if declared_suffix in {".doc", ".xls", ".ppt", ".msg"} else ".doc"
    if "WordDocument" in names:
        return ".doc"
    if "Workbook" in names or "Book" in names:
        return ".xls"
    if "PowerPoint Document" in names:
        return ".ppt"
    if "__properties_version1.0" in names:
        return ".msg"
    return declared_suffix


def _sniff_zip(path: Path, declared_suffix: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "mimetype" in names:
                kind = archive.read("mimetype").decode("ascii", "replace").strip()
                if kind == "application/epub+zip":
                    return ".epub"
                if kind.startswith("application/vnd.oasis.opendocument"):
                    return {"text": ".odt", "spreadsheet": ".ods", "presentation": ".odp"}.get(
                        kind.rsplit(".", 1)[-1], ".odt"
                    )
    except (zipfile.BadZipFile, OSError):
        return declared_suffix
    if any(name.startswith("word/") for name in names):
        return ".docx"
    if any(name.startswith("xl/") for name in names):
        return ".xlsx"
    if any(name.startswith("ppt/") for name in names):
        return ".pptx"
    return ".zip"


# --------------------------------------------------------- tier 1: native

def _from_pdf(path: Path, ctx: ConversionContext) -> Conversion:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = list(reader.pages)
    except Exception as error:
        return _from_pdf_pdfminer(path, ctx, error)

    chunks: list[str] = []
    for number, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as error:  # one broken page must not lose the document
            text = f"_(第 {number} 页解析失败：{error})_"
        chunks.append(f"## 第 {number} 页\n\n{text.strip() or '_(本页无可提取文本)_'}")
    body = "\n\n".join(chunks)
    dense = len(re.sub(r"\s+", "", body))
    suspect = dense < 40 * max(len(pages), 1)
    result = Conversion(body, "pypdf", pages=len(pages), ocr_suspect=suspect)
    if suspect:
        result.note = _ocr_note()
    return result


def _from_pdf_pdfminer(path: Path, ctx: ConversionContext, cause: Exception | None = None) -> Conversion:
    from pdfminer.high_level import extract_text

    body = extract_text(str(path)) or ""
    result = Conversion(body, "pdfminer", ocr_suspect=len(re.sub(r"\s+", "", body)) < 40)
    if cause is not None:
        result.note = f"pypdf 失败({type(cause).__name__})，已改用 pdfminer"
    if result.ocr_suspect:
        result.note = (result.note + "；" if result.note else "") + _ocr_note()
    return result


def _ocr_note() -> str:
    if shutil.which("tesseract"):
        return "疑似扫描件，可 OCR"
    return "疑似扫描件，但未安装 tesseract，无法 OCR"


def _docx_blocks(document):
    """Yield paragraphs and tables in true document order."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def docx_to_markdown(path: Path) -> str:
    """Structure-preserving DOCX rendering, shared by the native and LO paths."""
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    parts: list[str] = []
    for block in _docx_blocks(document):
        if isinstance(block, Table):
            parts.append(_table([[cell.text.strip() for cell in row.cells] for row in block.rows]))
            continue
        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name or "").lower()
        if style.startswith("heading") or style.startswith("标题"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            parts.append(f"{'#' * min(int(level), 6)} {text}")
        elif style.startswith("list") or style.startswith("列表"):
            parts.append(f"- {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


def _from_docx(path: Path, ctx: ConversionContext) -> Conversion:
    return Conversion(docx_to_markdown(path), "python-docx")


def _from_xlsx(path: Path, ctx: ConversionContext) -> Conversion:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for sheet in workbook.worksheets:
            rows: list[list[object]] = []
            truncated = False
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_SHEET_ROWS:
                    truncated = True
                    break
                rows.append(list(row[:MAX_TABLE_COLS]))
            heading = f"## 工作表：{sheet.title}"
            if truncated:
                heading += f"（仅前 {MAX_SHEET_ROWS} 行）"
            blocks.append(f"{heading}\n\n{_table(rows)}")
        return Conversion("\n\n".join(blocks), "openpyxl")
    finally:
        workbook.close()


def _from_xls(path: Path, ctx: ConversionContext) -> Conversion:
    """Legacy BIFF .xls via xlrd (2.x supports only .xls)."""
    import xlrd

    book = xlrd.open_workbook(str(path))
    blocks: list[str] = []
    for sheet in book.sheets():
        rows: list[list[object]] = []
        truncated = sheet.nrows > MAX_SHEET_ROWS
        for index in range(min(sheet.nrows, MAX_SHEET_ROWS)):
            rows.append([sheet.cell_value(index, col) for col in range(min(sheet.ncols, MAX_TABLE_COLS))])
        heading = f"## 工作表：{sheet.name}"
        if truncated:
            heading += f"（仅前 {MAX_SHEET_ROWS} 行）"
        blocks.append(f"{heading}\n\n{_table(rows)}")
    return Conversion("\n\n".join(blocks), "xlrd")


def _from_pptx(path: Path, ctx: ConversionContext) -> Conversion:
    """PowerPoint via python-pptx: slides, body levels, tables, speaker notes."""
    from pptx import Presentation

    presentation = Presentation(str(path))
    blocks: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        lines = [f"## 第 {number} 张幻灯片"]
        for shape in slide.shapes:
            if shape.has_table:
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                lines.append(_table(rows))
                continue
            if not shape.has_text_frame:
                continue
            title = shape == slide.shapes.title
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if not text:
                    continue
                level = paragraph.level or 0
                if title:
                    lines.append(f"### {text}")
                else:
                    lines.append(("  " * level) + f"- {text}")
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"> 备注：{notes}")
        blocks.append("\n\n".join(lines))
    return Conversion("\n\n\n".join(blocks), "python-pptx", pages=len(presentation.slides))


def _from_csv(path: Path, ctx: ConversionContext) -> Conversion:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [row for _, row in zip(range(MAX_SHEET_ROWS + 1), csv.reader(io.StringIO(raw), dialect))]
    truncated = len(rows) > MAX_SHEET_ROWS
    table = _table([list(row[:MAX_TABLE_COLS]) for row in rows[:MAX_SHEET_ROWS]])
    return Conversion(table + (f"\n\n_（仅前 {MAX_SHEET_ROWS} 行）_" if truncated else ""), "csv")


def _from_rtf(path: Path, ctx: ConversionContext) -> Conversion:
    from striprtf.striprtf import rtf_to_text

    raw = path.read_text(encoding="utf-8", errors="replace")
    return Conversion(rtf_to_text(raw), "striprtf")


def _from_text(path: Path, ctx: ConversionContext) -> Conversion:
    return Conversion(path.read_text(encoding="utf-8", errors="replace"), "text")


def _from_image(path: Path, ctx: ConversionContext) -> Conversion:
    if not shutil.which("tesseract"):
        return Conversion("", "image", note="图片，随问题走视觉通道（未装 tesseract，不做 OCR）")
    try:
        import pytesseract
        from PIL import Image

        text = pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng")
        return Conversion(text, "tesseract-ocr", note="由 OCR 提取")
    except Exception as error:
        return Conversion("", "image", note=f"OCR 失败：{type(error).__name__}: {error}")


NATIVE = {
    ".pdf": _from_pdf,
    ".docx": _from_docx,
    ".xlsx": _from_xlsx,
    ".xlsm": _from_xlsx,
    ".xls": _from_xls,
    ".pptx": _from_pptx,
    ".csv": _from_csv,
    ".tsv": _from_csv,
    ".rtf": _from_rtf,
    ".txt": _from_text,
    ".md": _from_text,
    ".markdown": _from_text,
    ".json": _from_text,
    ".log": _from_text,
}


# ------------------------------------------------- tier 2: MarkItDown backstop

_MARKITDOWN = None


def _markitdown():
    global _MARKITDOWN
    if _MARKITDOWN is None:
        from markitdown import MarkItDown

        _MARKITDOWN = MarkItDown(enable_plugins=False)
    return _MARKITDOWN


def from_markitdown(path: Path) -> Conversion:
    result = _markitdown().convert(str(path))
    text = (result.text_content or "").strip()
    if not text:
        raise ValueError("MarkItDown 返回空内容")
    return Conversion(text, "markitdown")


# --------------------------------------------- tier 3: LibreOffice backstop

def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def libreoffice_convert(path: Path, target_suffix: str, ctx: ConversionContext) -> Path | None:
    """Convert one file with headless LibreOffice; returns the new path or None.

    The user profile is redirected into the library cache: LibreOffice otherwise
    writes to the OS application-data directory, which a confined workspace
    cannot reach.
    """
    soffice = find_soffice()
    if soffice is None:
        return None
    for stale in ctx.cache.glob("lo-out-*"):
        shutil.rmtree(stale, ignore_errors=True)
    outdir = Path(tempfile.mkdtemp(prefix="lo-out-", dir=str(ctx.cache)))
    profile = ctx.cache / "loprofile"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to", target_suffix.lstrip("."),
        "--outdir", str(outdir), str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    expected = outdir / (path.stem + target_suffix)
    if expected.exists() and expected.stat().st_size > 0:
        return expected
    if completed.returncode != 0:
        return None
    matches = list(outdir.glob(f"*{target_suffix}"))
    return matches[0] if matches else None


# ------------------------------------------------------------------ the chain

def convert(path: Path, declared_suffix: str, ctx: ConversionContext) -> Conversion:
    """Run the three-tier chain and return the first successful conversion.

    Order is native → LibreOffice → MarkItDown → plain-text fallback. LibreOffice
    is tried before MarkItDown for its own target formats because MarkItDown has
    no converter for legacy/ODF containers at all, so the attempt would only add
    latency and noise to the note.
    """
    kind = sniff(path, declared_suffix)
    attempts: list[str] = []

    native = NATIVE.get(kind)
    if native is None and kind in PASSTHROUGH_SUFFIXES:
        native = _from_text
    if native is None and kind in IMAGE_SUFFIXES:
        native = _from_image

    if native is not None:
        try:
            return native(path, ctx)
        except Exception as error:
            attempts.append(f"{kind} 原生解析失败：{type(error).__name__}: {error}")

    target = LIBREOFFICE_TARGETS.get(kind) or LIBREOFFICE_TARGETS.get(declared_suffix)
    if target is not None:
        converted = libreoffice_convert(path, target, ctx)
        if converted is not None:
            follow = NATIVE.get(target)
            if follow is not None:
                try:
                    result = follow(converted, ctx)
                    result.engine = f"libreoffice→{target.lstrip('.')}+{result.engine}"
                    result.note = ("；".join(attempts) + "；" if attempts else "") + "经 LibreOffice 转换"
                    return result
                except Exception as error:
                    attempts.append(f"LibreOffice 产物解析失败：{type(error).__name__}: {error}")
        else:
            attempts.append("LibreOffice 转换失败" + ("（未安装）" if find_soffice() is None else ""))

    if kind not in IMAGE_SUFFIXES:
        try:
            result = from_markitdown(path)
            if attempts:
                result.note = "；".join(attempts) + "；已改用 MarkItDown"
            return result
        except Exception as error:
            attempts.append(f"MarkItDown 失败：{type(error).__name__}: {error}")

    # Last resort: a text file whose extension we did not recognise.
    try:
        payload = path.read_bytes()
        if _is_probably_text(payload):
            return Conversion(
                payload.decode("utf-8", "replace"), "text",
                note=("；".join(attempts) + "；" if attempts else "") + "按纯文本兜底读取",
            )
    except OSError:
        pass

    return Conversion("", "none", note="；".join(attempts) or f"没有可用的转换器处理 {kind}")


# ------------------------------------------------------------------ ingestion

def _register(library: Library, entry: DocEntry) -> DocEntry:
    """Register one document, replacing any earlier entry with the same slug."""
    library.docs = [existing for existing in library.docs if existing.slug != entry.slug]
    library.docs.append(entry)
    library.write_index()
    return entry


def ingest_file(library: Library, source: Path | bytes, name: str | None = None) -> DocEntry:
    """Persist one uploaded file verbatim, convert it, and register it."""
    display = name or (str(source) if isinstance(source, Path) else "upload.bin")
    payload = source.read_bytes() if isinstance(source, Path) else bytes(source)
    slug = _slug(display, payload)
    declared = Path(display).suffix.lower()

    library.prepare()
    upload_path = library.uploads / slug
    upload_path.write_bytes(payload)

    entry = DocEntry(
        slug=slug,
        source_name=Path(display).name,
        suffix=declared,
        bytes=len(payload),
        upload_path=f"uploads/{slug}",
        markdown_path=None,
        chars=0,
    )

    if declared in IMAGE_SUFFIXES:
        result = convert(upload_path, declared, library.context())
        entry.engine = result.engine
        entry.note = result.note or "图片，随问题走视觉通道"
        return _register(library, entry)

    try:
        result = convert(upload_path, declared, library.context())
    except Exception as error:  # conversion must never lose the upload
        entry.note = f"转换失败：{type(error).__name__}: {error}"
        return _register(library, entry)

    entry.engine = result.engine
    entry.pages = result.pages
    entry.ocr_suspect = result.ocr_suspect

    if not result.text.strip():
        entry.note = result.note or "转换结果为空"
        return _register(library, entry)

    markdown_path = library.markdown / f"{Path(slug).stem}.md"
    markdown_path.write_text(result.text, encoding="utf-8")
    entry.markdown_path = f"docs/{markdown_path.name}"
    entry.chars = len(result.text)
    entry.note = result.note
    return _register(library, entry)


def ingest_paths(library: Library, paths: list[Path]) -> list[DocEntry]:
    return [ingest_file(library, path) for path in paths]


def reset_library(library: Library, keep_home: bool = True) -> None:
    """Drop uploaded documents (and optionally the DSH home) from disk."""
    shutil.rmtree(library.workspace, ignore_errors=True)
    shutil.rmtree(library.cache, ignore_errors=True)
    if not keep_home:
        shutil.rmtree(library.home, ignore_errors=True)
    library.docs = []
    library.prepare()


def engine_availability() -> dict[str, bool]:
    """Report which tiers of the chain are usable in this environment."""
    available = {
        "libreoffice": find_soffice() is not None,
        "tesseract": shutil.which("tesseract") is not None,
        "pandoc": shutil.which("pandoc") is not None,
    }
    for module in ("pypdf", "pdfminer", "docx", "openpyxl", "xlrd", "pptx", "markitdown", "striprtf", "olefile"):
        try:
            __import__(module)
            available[module] = True
        except ImportError:
            available[module] = False
    return available
