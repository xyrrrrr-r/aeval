"""Python side of the DSH session bridge (plan §3).

Python NEVER parses DSH session files. The only read path is the
official ``JsonlSessionPersistence.open(SessionId(id), 'read')`` +
``handle.read()`` inside the Node tool ``tools/dsh-session-reader``
(the TS bridge). This module:

- spawns the bridge with one JSON request on stdin,
- reads exactly ONE JSON envelope from stdout,
- maps structured failures to typed exceptions,
- classifies every failure as infra_invalid (never score-relevant).

Protocol (frozen):
    request : {"protocolVersion":1,"requestId":uuid,
               "operation":"read","sessionId":opaque-id}
    success : {"protocolVersion":1,"requestId":uuid,"ok":true,
               "result":{"header":{...},"inheritedEventCount":0,
                          "eventState":"...","events":[...]}}
    failure : {"protocolVersion":1,"requestId":uuid,"ok":false,
               "error":{"code":"DSH_READ_FAILED",...}}  + nonzero exit
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "DshReaderFailure",
    "DshBridgeProtocolError",
    "DshReaderRequest",
    "DshReaderResponse",
    "read_dsh_session_via_bridge",
    "parse_dsh_reader_envelope",
]

BRIDGE_PROTOCOL_VERSION = 1

BRIDGE_ERROR_CODES = (
    "INVALID_REQUEST",
    "SOURCE_ROOT_DENIED",
    "SESSION_NOT_FOUND",
    "DSH_OPEN_FAILED",
    "DSH_READ_FAILED",
    "SERIALIZATION_FAILED",
    "INTERNAL",
)


class DshReaderFailure(RuntimeError):
    """Bridge returned a structured error → trial is infra_invalid."""

    def __init__(self, code: str, message: str, request_id: str | None = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        super().__init__(f"DSH bridge error {code}: {message}")


class DshBridgeProtocolError(RuntimeError):
    """Bridge violated the protocol (non-JSON, multiple envelopes, wrong id).

    Also infra_invalid: a protocol violation means we cannot trust what
    we read, so we must never grade on top of it.
    """


@dataclass(frozen=True)
class DshReaderRequest:
    session_id: str
    bridge_path: Path
    allowed_base: Path
    source_root: Path
    timeout_seconds: float = 120.0

    def to_payload(self, request_id: str) -> dict[str, Any]:
        return {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "requestId": request_id,
            "operation": "read",
            "sessionId": self.session_id,
        }


@dataclass(frozen=True)
class DshReaderResponse:
    request_id: str
    header: dict[str, Any]
    inherited_event_count: int
    event_state: str
    events: list[dict[str, Any]]


def parse_dsh_reader_envelope(stdout: bytes, expected_request_id: str) -> DshReaderResponse:
    """Validate the single stdout envelope from the bridge.

    Enforces: exactly one JSON document, correct protocol version,
    matching requestId, and on success all four result fields present.
    """
    try:
        text = stdout.decode("utf-8", errors="strict")
        envelope = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DshBridgeProtocolError(
            f"bridge stdout is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(envelope, dict):
        raise DshBridgeProtocolError("bridge envelope must be a JSON object")

    if type(envelope.get("protocolVersion")) is not int or envelope["protocolVersion"] != BRIDGE_PROTOCOL_VERSION:
        raise DshBridgeProtocolError(
            f"bridge protocolVersion: expected {BRIDGE_PROTOCOL_VERSION}, "
            f"got {envelope.get('protocolVersion')!r}"
        )
    if envelope.get("requestId") != expected_request_id:
        raise DshBridgeProtocolError(
            f"bridge requestId mismatch: expected {expected_request_id!r}, "
            f"got {envelope.get('requestId')!r}"
        )

    if type(envelope.get("ok")) is not bool:
        raise DshBridgeProtocolError("bridge ok must be a boolean")
    if not envelope["ok"]:
        error = envelope.get("error")
        if not isinstance(error, dict):
            raise DshBridgeProtocolError(
                "bridge error envelope missing the error object"
            )
        code = error.get("code")
        if code not in BRIDGE_ERROR_CODES:
            raise DshBridgeProtocolError(f"bridge returned unknown error code {code!r}")
        raise DshReaderFailure(
            code=code,
            message=str(error.get("message", "")),
            request_id=expected_request_id,
        )

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise DshBridgeProtocolError("bridge success envelope missing result")
    for field in ("header", "eventState", "events"):
        if field not in result:
            raise DshBridgeProtocolError(f"bridge result missing {field!r}")
    header = result["header"]
    events = result["events"]
    if not isinstance(header, dict):
        raise DshBridgeProtocolError("bridge result.header must be an object")
    if not isinstance(events, list):
        raise DshBridgeProtocolError("bridge result.events must be an array")

    return DshReaderResponse(
        request_id=expected_request_id,
        header=header,
        inherited_event_count=int(result.get("inheritedEventCount", 0)),
        event_state=str(result["eventState"]),
        events=[e if isinstance(e, dict) else {"raw": e} for e in events],
    )


def read_dsh_session_via_bridge(request: DshReaderRequest) -> DshReaderResponse:
    """Run the TS bridge and return the official session read.

    Failure modes all raise; the caller (trial pipeline) maps every
    exception to infra_invalid. No partial results are ever returned.
    """
    if not request.bridge_path.is_file():
        raise DshBridgeProtocolError(
            f"DSH session reader bridge not found: {request.bridge_path} "
            "(build tools/dsh-session-reader first)"
        )
    request_id = str(uuid4())
    payload = json.dumps(request.to_payload(request_id), ensure_ascii=False)

    try:
        completed = subprocess.run(
            [
                "node",
                str(request.bridge_path),
                "--allowed-base",
                str(request.allowed_base.resolve()),
                "--source-root",
                str(request.source_root.resolve()),
            ],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=request.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise DshBridgeProtocolError("node runtime not found for the DSH bridge") from exc
    except subprocess.TimeoutExpired as exc:
        raise DshBridgeProtocolError(
            f"DSH bridge timed out after {request.timeout_seconds}s"
        ) from exc

    if completed.returncode != 0:
        # Nonzero exit must carry a structured error envelope on stdout;
        # a bare crash without one is a protocol violation.
        try:
            parse_dsh_reader_envelope(completed.stdout.encode("utf-8"), request_id)
        except DshReaderFailure:
            raise
        except DshBridgeProtocolError as exc:
            raise DshBridgeProtocolError(
                f"bridge exited {completed.returncode} without a structured "
                f"error envelope: {exc}; stderr={completed.stderr[:500]!r}"
            ) from exc

    return parse_dsh_reader_envelope(completed.stdout.encode("utf-8"), request_id)
