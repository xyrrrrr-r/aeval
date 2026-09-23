"""Builders for the pinned DSH 0.1.7-alpha.1 Session V4 log shape.

These emit the *official* event envelope (``{type, seq, time, data}`` plus
surface metadata) read from ``@deepseek-ai/dsh-session`` types — not the
legacy flat shapes an earlier mapper invented. Anything that would make a
real reader refuse must be expressed here explicitly.
"""

from __future__ import annotations

from typing import Any

PROVIDER = "deepseek-official"
MODEL = "deepseek-v4-flash"
STORAGE_VERSION = 4
START_MS = 1_730_000_000_000


def session_header(
    *,
    session_id: str = "session-under-test",
    seeded: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    header: dict[str, Any] = {
        "version": STORAGE_VERSION,
        "id": session_id,
        "createdAt": START_MS,
        "isSeeded": seeded,
    }
    header.update(overrides)
    return header


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def model_source(provider: str = PROVIDER, model: str = MODEL) -> dict[str, Any]:
    return {"kind": "model", "provider": provider, "model": model}


def usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 5,
    cache_write: int = 0,
    total: int | None = None,
) -> dict[str, Any]:
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
        "totalTokens": total if total is not None else input_tokens + cache_read + cache_write + output_tokens,
    }


class SessionLog:
    """Append-only builder: sequence numbers and timestamps stay contiguous."""

    def __init__(self, *, start_ms: int = START_MS + 1000):
        self.events: list[dict[str, Any]] = []
        self._time = start_ms
        self._ids = {"system": 0, "user": 0, "assistant": 0, "tool": 0, "developer": 0}
        self.inherited = 0

    # -- plumbing ---------------------------------------------------------
    def raw(
        self, etype: str, data: Any, *, surface_op: Any = None, source_seqs: list[int] | None = None,
        ignorable: bool = False, at: int | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": etype, "seq": len(self.events) if at is None else at,
            "time": self._time, "data": data,
        }
        if surface_op is not None:
            event["surfaceOp"] = surface_op
        if source_seqs is not None:
            event["sourceEventSeqs"] = source_seqs
        if ignorable:
            event["ignorable"] = True
        self._time += 1000
        self.events.append(event)
        return event

    def _id(self, role: str) -> str:
        self._ids[role] += 1
        return f"{role}-{self._ids[role]}"

    # -- lifecycle --------------------------------------------------------
    def request_header(
        self, *, reason: str = "initial", model: str = MODEL, effort: str | None = None,
        tools: list[dict[str, Any]] | None = None, provider: str = PROVIDER, starts_series: bool = False,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"provider": provider, "model": model}
        if effort is not None:
            config["reasoningEffort"] = effort
        header: dict[str, Any] = {"config": config}
        if tools is not None:
            header["tools"] = tools
        data: dict[str, Any] = {"header": header, "reason": reason}
        if starts_series:
            data["startsSeries"] = True
        return self.raw("request/header", data)

    def turn_start(self, turn: int) -> dict[str, Any]:
        return self.raw("turn/start", {"turn": turn})

    def turn_end(self, turn: int, kind: str = "completed", **reason: Any) -> dict[str, Any]:
        return self.raw("turn/end", {"turn": turn, "reason": {"kind": kind, **reason}})

    def step_start(self, turn: int, step: int) -> dict[str, Any]:
        return self.raw("step/start", {"turn": turn, "step": step})

    def step_end(self, turn: int, step: int) -> dict[str, Any]:
        return self.raw("step/end", {"turn": turn, "step": step})

    def end_seed(self, *, inherited: bool = True) -> dict[str, Any]:
        event = self.raw("session/end-seed", {"inherited": True} if inherited else {})
        # Official rule: Session reads log[inheritedEventCount] as the marker, so
        # the count is the marker's own seq and the prefix is what precedes it.
        self.inherited = event["seq"]
        return event

    # -- messages ---------------------------------------------------------
    def system_message(self, turn: int, step: int, text: str = "system prompt") -> dict[str, Any]:
        message = {
            "id": self._id("system"), "role": "system", "content": [text_block(text)],
            "source": {"kind": "system-prompt"},
        }
        return self.raw(
            "system/message", {"turn": turn, "step": step, "message": message}, surface_op="append"
        )

    def user_message(self, text: str = "solve the task") -> dict[str, Any]:
        message = {
            "id": self._id("user"), "role": "user", "content": [text_block(text)],
            "source": {"kind": "user"},
        }
        return self.raw("user/message", message, surface_op="append")

    def developer_message(
        self, turn: int, step: int, blocks: list[dict[str, Any]], *, header_seq: int | None = None
    ) -> dict[str, Any]:
        message = {
            "id": self._id("developer"), "role": "developer", "content": blocks,
            "source": {"kind": "notice"},
        }
        data: dict[str, Any] = {"turn": turn, "step": step, "message": message}
        if header_seq is not None:
            data["headerSeq"] = header_seq
        return self.raw("developer/message", data, surface_op="append")

    def assistant_message(
        self, turn: int, step: int, text: str = "", *, usage_report: dict[str, Any] | None = None,
        blocks: list[dict[str, Any]] | None = None, stream: list[dict[str, Any]] | None = None,
        interrupted: bool = False, model: str = MODEL,
    ) -> dict[str, Any]:
        content = blocks if blocks is not None else ([text_block(text)] if text else [])
        message = {
            "id": self._id("assistant"), "role": "assistant", "content": content,
            "source": model_source(model=model),
        }
        data: dict[str, Any] = {
            "turn": turn, "step": step, "message": message,
            "stream": stream if stream is not None else [],
        }
        if usage_report is not None:
            data["usage"] = usage_report
        if interrupted:
            data["interrupted"] = True
        return self.raw("assistant/message", data, surface_op="append")

    def assistant_attempt(self, turn: int, step: int) -> dict[str, Any]:
        return self.raw("assistant/attempt", {"turn": turn, "step": step, "stream": []})

    # -- tools ------------------------------------------------------------
    def tool_call(
        self, turn: int, step: int, call_id: str, name: str = "bash", arguments: Any = '{"cmd":"ls"}'
    ) -> dict[str, Any]:
        raw_arguments = arguments if isinstance(arguments, str) else _json(arguments)
        return self.raw(
            "tool/call", {"turn": turn, "step": step, "callId": call_id, "name": name, "arguments": raw_arguments}
        )

    def tool_result(
        self, turn: int, step: int, call_id: str, text: str = "ok", *, is_error: bool = False,
        surface_op: Any = "append", source_seqs: list[int] | None = None, message_id: str | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": message_id or self._id("tool"), "role": "tool", "toolCallId": call_id,
            "content": [text_block(text)], "source": {"kind": "tool", "callId": call_id},
        }
        if is_error:
            message["isError"] = True
        data: dict[str, Any] = {"turn": turn, "step": step, "message": message}
        return self.raw("tool/result", data, surface_op=surface_op, source_seqs=source_seqs)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def happy_log(**kwargs: Any) -> SessionLog:
    """One complete two-step turn: report + tool use, every step settled."""
    log = SessionLog()
    log.request_header(**kwargs)
    log.system_message(0, 0)
    log.turn_start(0)
    log.user_message()
    log.step_start(0, 0)
    log.assistant_message(0, 0, "I will list the files.", usage_report=usage())
    log.tool_call(0, 0, "call-1", "bash", {"cmd": "ls"})
    log.tool_result(0, 0, "call-1", "answer.txt")
    log.step_end(0, 0)
    log.step_start(0, 1)
    log.assistant_message(0, 1, "The answer is in answer.txt.", usage_report=usage(input_tokens=140, output_tokens=12))
    log.step_end(0, 1)
    log.turn_end(0, "completed")
    return log


def seeded_log(**kwargs: Any) -> SessionLog:
    """A resumed session: an inherited turn, then one complete live turn.

    ``log.inherited`` is the marker position, which the header's ``isSeeded``
    and the reader's ``inheritedEventCount`` must both agree with.
    """
    log = SessionLog()
    log.request_header(**kwargs)
    log.system_message(0, 0)
    log.turn_start(0)
    log.user_message("earlier question")
    log.step_start(0, 0)
    log.assistant_message(0, 0, "earlier answer", usage_report=usage())
    log.tool_call(0, 0, "past-call", "bash", {"cmd": "ls"})
    log.tool_result(0, 0, "past-call", "was here")
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    log.end_seed()
    log.request_header(reason="resume", **kwargs)
    log.turn_start(1)
    log.user_message("live question")
    log.step_start(1, 0)
    log.assistant_message(1, 0, "live answer", usage_report=usage(input_tokens=140, output_tokens=12))
    log.step_end(1, 0)
    log.turn_end(1, "completed")
    return log
