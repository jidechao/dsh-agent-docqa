"""Command-line surface for the document Q&A engine (no UI required)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .engine import DocQAEngine, EngineConfig, PERMISSION_MODES, resolve_permission_mode
from .envfile import load_env_file
from .events import TurnEvent
from .ingest import reset_library


def _engine(args: argparse.Namespace) -> DocQAEngine:
    return DocQAEngine(EngineConfig(
        library_root=Path(args.library).expanduser().resolve(),
        provider=args.provider,
        model=args.model,
        profile=args.profile,
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL"),
        permission_mode=args.permission_mode,
    ))


def main(argv: list[str] | None = None) -> int:
    # 项目根目录的 .env：与 run_web.py 共用同一份配置（--model 等默认值也从它来）
    load_env_file(Path(os.environ.get("DOCQA_ENV_FILE") or Path(__file__).resolve().parent.parent / ".env"))
    parser = argparse.ArgumentParser(prog="docqa", description="个人文档问答助手（DSH Python SDK）")
    parser.add_argument("--library", default=".docqa", help="资料库目录（默认 ./.docqa）")
    parser.add_argument("--provider", default="deepseek-official")
    parser.add_argument("--model", default=os.environ.get("DSH_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash")
    parser.add_argument("--profile", default=os.environ.get("DOCQA_PROFILE", "sdk"))
    parser.add_argument(
        "--permission-mode", choices=PERMISSION_MODES,
        default=resolve_permission_mode(
            os.environ.get("DOCQA_PERMISSION_MODE"), os.environ.get("DSH_PERMISSION_MODE")
        ),
        help="dsh 权限预设（默认跟随 dsh：workspace-write）。Windows 打包运行时下，"
             "只有 danger-full-access 能绕开无法启动的 ACL runner，shell/检索工具才可用",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="上传并转换文档")
    ingest.add_argument("files", nargs="+", type=Path)

    ask = sub.add_parser("ask", help="针对已上传文档提问")
    ask.add_argument("question")
    ask.add_argument("--new-session", action="store_true", help="开一段新对话")

    sub.add_parser("reset", help="清空资料库（保留 SDK home）")
    sub.add_parser("status", help="显示资料库与会话状态")
    sub.add_parser("engines", help="显示三层转换链在当前环境里的可用性")

    args = parser.parse_args(argv)
    engine = _engine(args)

    if args.command == "engines":
        from .ingest import engine_availability

        tiers = {
            "libreoffice": "第 2 层 LibreOffice（.doc/.ppt/.xls/.odt/.wps 后备）",
            "markitdown": "第 3 层 MarkItDown（.epub/.msg/.ipynb/.html/.xml）",
            "tesseract": "扫描件 OCR",
            "pandoc": "pandoc（未使用，仅供参考）",
        }
        native = {
            "pypdf": ".pdf", "pdfminer": ".pdf（备用）", "docx": ".docx",
            "openpyxl": ".xlsx", "xlrd": ".xls", "pptx": ".pptx",
            "striprtf": ".rtf", "olefile": "魔数嗅探（OLE2 判别）",
        }
        availability = engine_availability()
        print("第 1 层 原生解析：")
        for name, what in native.items():
            print(f"  {'✓' if availability.get(name) else '✗'} {name:<10} {what}")
        print("后备层：")
        for name, what in tiers.items():
            print(f"  {'✓' if availability.get(name) else '✗'} {name:<12} {what}")
        return 0

    if args.command == "reset":
        reset_library(engine.library)
        print(f"已清空 {engine.library.workspace}")
        return 0

    if args.command == "status":
        print(f"library    : {engine.library.root}")
        print(f"session id : {engine.session_id}")
        print(f"documents  : {len(engine.library.docs)} ({engine.library.total_chars():,} 字符 Markdown)")
        for entry in engine.library.docs:
            print(f"  - {entry.source_name} → {entry.markdown_path or '(未转换)'} {entry.note}")
        return 0

    if args.command == "ingest":
        for path in args.files:
            entry = engine.add_document(path)
            status = entry.markdown_path or "未转换"
            print(f"[{entry.source_name}] {entry.bytes:,} B → {status} ({entry.chars:,} 字符) {entry.note}")
        return 0

    if args.new_session:
        print(f"new session: {engine.new_session()}")

    if not engine.library.docs:
        print("资料库为空，请先运行 `ingest`。", file=sys.stderr)
        return 2

    try:
        for event in engine.ask_stream(args.question):
            _print_event(event)
    finally:
        engine.close()
    return 0


def _print_event(event: TurnEvent) -> None:
    if event.kind == "done":
        print("\n" + event.text)
        print(f"\n[finish_reason={event.data.get('finish_reason')} "
              f"耗时 {event.data.get('elapsed_ms', 0) / 1000:.1f}s]")
    elif event.kind == "error":
        print(f"\n[error] {event.text}", file=sys.stderr)
    elif event.kind == "text":
        print(f"\n[assistant] {event.text}")
    elif event.kind in {"thinking", "tool_call", "tool_result"}:
        print(f"  {event.text}")
    else:
        print(f"[{event.kind}] {event.text}")


if __name__ == "__main__":
    raise SystemExit(main())
