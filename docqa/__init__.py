"""个人文档问答助手：DSH Python SDK + 本地文档转换 + 流式事件契约。"""

from .engine import DocQAEngine, EngineConfig
from .events import TurnEvent, TurnTranslator, summarize_arguments
from .ingest import DocEntry, Library, engine_availability, ingest_file, ingest_paths, reset_library

__all__ = [
    "DocQAEngine",
    "EngineConfig",
    "TurnEvent",
    "TurnTranslator",
    "summarize_arguments",
    "DocEntry",
    "Library",
    "engine_availability",
    "ingest_file",
    "ingest_paths",
    "reset_library",
]
