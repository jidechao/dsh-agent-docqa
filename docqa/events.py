"""Typed turn events: the one streaming contract shared by CLI, SSE API, and UI.

The DeepSeek Harness Python SDK does not stream tokens. Its notification surface
carries only durable session events (``session.event``) plus ``session.status``,
so a turn arrives as a sequence of *committed* facts rather than deltas:

======================  ==========================================  ===========
event                   meaning                                     realtime?
======================  ==========================================  ===========
``turn_start``          the agent began a turn                      yes
``step_start``          one model step began                       yes
``thinking``            a reasoning block of a committed step       per step
``text``                visible text of a committed step            per step
``tool_call``           the model requested a tool, args included   yes
``tool_result``         the tool returned                           yes
``step_end``            one model step finished                     yes
``turn_end``            the turn finished, with its reason          yes
``status``              whole-agent idle/running                    yes
``done`` / ``error``    our own terminal marker for the request      —
======================  ==========================================  ===========

``tool_call`` is appended *before* the tool executes and ``tool_result`` after,
so those two are genuinely live and are what a UI should show while the model is
still working. Thinking and answer text arrive when their step commits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: Tool names whose first argument is worth surfacing instead of raw JSON.
_PRIMARY_ARGUMENT = {
    "read": "file_path", "write": "file_path", "edit": "file_path",
    "grep": "pattern", "glob": "pattern",
    "pwsh": "command", "bash": "command",
    "read_document": "file_path",
    "list_agents": None, "todo_write": None,
}

PREVIEW_CHARS = 600


@dataclass(slots=True)
class TurnEvent:
    """One streamed fact about a running turn."""

    kind: str
    data: dict = field(default_factory=dict)
    seq: int = 0

    def to_sse(self) -> str:
        payload = json.dumps({"kind": self.kind, "seq": self.seq, **self.data}, ensure_ascii=False)
        # ``data:`` must stay on one line for the SSE framing to survive.
        return f"id: {self.seq}\nevent: {self.kind}\ndata: {payload}\n\n"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "seq": self.seq, **self.data}

    @property
    def text(self) -> str:
        """One-line human summary, used by the CLI and by logs."""
        data = self.data
        if self.kind == "turn_start":
            return f"开始处理（第 {data.get('turn', '?')} 轮）"
        if self.kind == "step_start":
            return f"模型第 {data.get('step', '?')} 步"
        if self.kind == "step_end":
            return f"第 {data.get('step', '?')} 步结束"
        if self.kind == "thinking":
            return f"[思考] {_clip(data.get('text', ''), 120)}"
        if self.kind == "text":
            return f"[输出] {_clip(data.get('text', ''), 120)}"
        if self.kind == "tool_call":
            return f"调用 {data.get('name', '?')}：{data.get('summary', '')}"
        if self.kind == "tool_result":
            mark = "失败" if data.get("ok") is False else "返回"
            return f"工具{mark}（{data.get('name') or 'tool'}）：{_clip(data.get('preview', ''), 100)}"
        if self.kind == "turn_end":
            return f"本轮结束：{data.get('reason', 'unknown')}"
        if self.kind == "status":
            return f"agent 状态：{data.get('status')}"
        if self.kind == "done":
            return data.get("answer", "") or "（无输出）"
        if self.kind == "error":
            return data.get("message", "未知错误")
        return self.kind


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def summarize_arguments(name: str, raw: object) -> str:
    """Turn a raw tool-argument string into a short, readable line."""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    key = _PRIMARY_ARGUMENT.get(name, "__first__")
    if key is not None:
        try:
            parsed = json.loads(text) if isinstance(text, str) else raw
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed:
            if key == "__first__":
                key = next(iter(parsed))
            value = parsed.get(key)
            if value is not None:
                extra = ""
                if name == "grep" and parsed.get("path"):
                    extra = f"  @ {parsed['path']}"
                return _clip(f"{key}={value}{extra}", 180)
    return _clip(text, 180)


def _blocks_to_parts(content: object) -> tuple[str, str]:
    """Split a content-block list into (reasoning, visible text)."""
    reasoning: list[str] = []
    visible: list[str] = []
    if not isinstance(content, list):
        return "", ""
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "reasoning":
            reasoning.append(str(block.get("text") or ""))
        elif kind == "text":
            visible.append(str(block.get("text") or ""))
    return "".join(reasoning), "".join(visible)


def _tool_result_preview(data: dict) -> tuple[str, bool | None]:
    """Extract display text and error state from a ``tool/result`` payload."""
    message = data.get("message")
    block = None
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            block = content[0]
    if block is None:
        return "", None
    _, visible = _blocks_to_parts(block.get("content"))
    is_error = block.get("isError")
    error = data.get("error")
    if isinstance(error, dict) and error.get("name"):
        is_error = True
    return _clip(visible, PREVIEW_CHARS), (True if is_error else False)


class TurnTranslator:
    """Stateful mapper from SDK notifications to :class:`TurnEvent` values.

    Stateful because ``tool/result`` carries only a ``toolCallId``; the tool name
    has to be remembered from the matching ``tool/call``.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._calls: dict[str, str] = {}
        self._seq = 0

    def _emit(self, kind: str, **data: object) -> TurnEvent:
        self._seq += 1
        return TurnEvent(kind=kind, data=data, seq=self._seq)

    def feed(self, notification) -> list[TurnEvent]:
        method = getattr(notification, "method", None)
        payload = getattr(notification, "payload", None)
        if not isinstance(payload, dict):
            return []
        if method == "session.status":
            if payload.get("sessionId") != self.session_id:
                return []
            return [self._emit("status", status=payload.get("status"))]
        if method != "session.event":
            return []
        if payload.get("sessionId") != self.session_id:
            return []
        event = payload.get("event")
        if not isinstance(event, dict):
            return []
        return self._from_session_event(event)

    def _from_session_event(self, event: dict) -> list[TurnEvent]:
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        turn = data.get("turn")
        step = data.get("step")

        if kind == "turn/start":
            return [self._emit("turn_start", turn=turn)]
        if kind == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            return [self._emit("turn_end", turn=turn, reason=reason.get("kind", "unknown"))]
        if kind == "step/start":
            return [self._emit("step_start", turn=turn, step=step)]
        if kind == "step/end":
            return [self._emit("step_end", turn=turn, step=step)]

        if kind == "tool/call":
            call_id = str(data.get("callId") or "")
            name = str(data.get("name") or "tool")
            self._calls[call_id] = name
            return [self._emit(
                "tool_call", call_id=call_id, name=name, turn=turn, step=step,
                arguments=data.get("arguments") if isinstance(data.get("arguments"), str) else "",
                summary=summarize_arguments(name, data.get("arguments")),
            )]

        if kind == "tool/result":
            message = data.get("message")
            call_id = ""
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    call_id = str(content[0].get("toolCallId") or "")
            preview, is_error = _tool_result_preview(data)
            return [self._emit(
                "tool_result", call_id=call_id, name=self._calls.get(call_id, "tool"),
                turn=turn, step=step, ok=not is_error, preview=preview, chars=len(preview),
            )]

        if kind == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            reasoning, visible = _blocks_to_parts(message.get("content"))
            events: list[TurnEvent] = []
            if reasoning.strip():
                events.append(self._emit("thinking", text=reasoning, turn=turn, step=step))
            if visible.strip():
                events.append(self._emit("text", text=visible, turn=turn, step=step))
            return events

        return []
