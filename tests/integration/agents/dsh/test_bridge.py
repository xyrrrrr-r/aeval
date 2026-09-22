"""Python↔TS bridge protocol tests (plan §7 rows 6–7).

The real TS bridge (official JsonlSessionPersistence) is M3; these
tests freeze the Python side of the protocol with a stub node process:
Python must parse exactly one envelope, map structured errors to
typed exceptions, and classify every failure infra_invalid.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from aeval.agents.dsh.bridge import (
    BRIDGE_PROTOCOL_VERSION,
    DshBridgeProtocolError,
    DshReaderFailure,
    DshReaderRequest,
    parse_dsh_reader_envelope,
    read_dsh_session_via_bridge,
)

RID = "req-1"


def _ok_envelope(**result_overrides) -> bytes:
    result = {
        "header": {"sessionId": "s-1", "agent": "dsh"},
        "inheritedEventCount": 2,
        "eventState": "shared-frozen",
        "events": [{"type": "message", "source": "user", "text": "hi"}],
    }
    result.update(result_overrides)
    return json.dumps({
        "protocolVersion": BRIDGE_PROTOCOL_VERSION,
        "requestId": RID,
        "ok": True,
        "result": result,
    }).encode("utf-8")


def _error_envelope(code="DSH_READ_FAILED") -> bytes:
    return json.dumps({
        "protocolVersion": BRIDGE_PROTOCOL_VERSION,
        "requestId": RID,
        "ok": False,
        "error": {"code": code, "message": "boom"},
    }).encode("utf-8")


def test_success_envelope_parses_with_all_fields():
    response = parse_dsh_reader_envelope(_ok_envelope(), RID)
    assert response.request_id == RID
    assert response.event_state == "shared-frozen"
    assert response.inherited_event_count == 2
    assert response.events[0]["type"] == "message"
    assert response.header == {"sessionId": "s-1", "agent": "dsh"}


def test_error_envelope_maps_to_typed_failure():
    with pytest.raises(DshReaderFailure) as excinfo:
        parse_dsh_reader_envelope(_error_envelope("SESSION_NOT_FOUND"), RID)
    assert excinfo.value.code == "SESSION_NOT_FOUND"
    assert excinfo.value.request_id == RID


def test_unknown_error_code_is_protocol_violation():
    with pytest.raises(DshBridgeProtocolError, match="unknown error code"):
        parse_dsh_reader_envelope(_error_envelope("SOMETHING_NEW"), RID)


@pytest.mark.parametrize("bad", [
    b"not json at all",
    b"\xff\xfe invalid utf8",
    b'{"protocolVersion":2, "requestId":RID, "ok":true, "result":{}}'.replace(b"RID", b'"req-1"'),
    json.dumps({"protocolVersion": 1, "requestId": "other-id", "ok": True,
                "result": {"header": {}, "eventState": "x", "events": []}}).encode(),
    json.dumps({"protocolVersion": 1, "requestId": RID, "ok": True,
                "result": {"header": {}, "events": []}}).encode(),  # no eventState
    json.dumps({"protocolVersion": 1, "requestId": RID, "ok": True,
                "result": {"eventState": "x", "events": []}}).encode(),  # no header
    json.dumps({"protocolVersion": 1, "requestId": RID, "ok": True,
                "result": {"header": {}, "eventState": "x"}}).encode(),  # no events
    json.dumps({"protocolVersion": 1, "requestId": RID, "ok": True}).encode(),  # no result
    _ok_envelope() + b"\n" + _ok_envelope(),  # two envelopes
])
def test_protocol_violations_rejected(bad):
    with pytest.raises(DshBridgeProtocolError):
        parse_dsh_reader_envelope(bad, RID)


def _request(tmp_path: Path, bridge_js: str, timeout=10.0) -> DshReaderRequest:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text(bridge_js, encoding="utf-8")
    return DshReaderRequest(
        session_id="s-1",
        bridge_path=bridge,
        allowed_base=tmp_path,
        source_root=tmp_path,
        timeout_seconds=timeout,
    )


def test_read_via_stub_bridge_roundtrip(tmp_path):
    request = _request(tmp_path, """
        let input = '';
        process.stdin.on('data', c => input += c);
        process.stdin.on('end', () => {
          const req = JSON.parse(input);
          process.stdout.write(JSON.stringify({
            protocolVersion: 1,
            requestId: req.requestId,
            ok: true,
            result: { header: {sessionId: req.sessionId},
                      inheritedEventCount: 0,
                      eventState: 'shared-frozen',
                      events: [{type: 'message', source: 'user', text: 'hello'}] },
          }));
        });
    """)
    response = read_dsh_session_via_bridge(request)
    assert response.events[0]["text"] == "hello"


def test_read_via_stub_bridge_structured_failure(tmp_path):
    request = _request(tmp_path, """
        let input = '';
        process.stdin.on('data', c => input += c);
        process.stdin.on('end', () => {
          const req = JSON.parse(input);
          process.stdout.write(JSON.stringify({
            protocolVersion: 1, requestId: req.requestId, ok: false,
            error: {code: 'SESSION_NOT_FOUND', message: 'no such session'},
          }));
          process.exit(3);
        });
    """)
    with pytest.raises(DshReaderFailure) as excinfo:
        read_dsh_session_via_bridge(request)
    assert excinfo.value.code == "SESSION_NOT_FOUND"


def test_read_via_stub_bridge_bare_crash_is_protocol_error(tmp_path):
    request = _request(tmp_path, "process.stderr.write('boom'); process.exit(1);")
    with pytest.raises(DshBridgeProtocolError, match="without a structured error"):
        read_dsh_session_via_bridge(request)


def test_read_timeout_is_protocol_error(tmp_path):
    request = _request(tmp_path, "setTimeout(() => {}, 60000);", timeout=0.5)
    with pytest.raises(DshBridgeProtocolError, match="timed out"):
        read_dsh_session_via_bridge(request)


def test_missing_bridge_file_is_protocol_error(tmp_path):
    request = DshReaderRequest(
        session_id="s-1",
        bridge_path=tmp_path / "nope.mjs",
        allowed_base=tmp_path,
        source_root=tmp_path,
    )
    with pytest.raises(DshBridgeProtocolError, match="not found"):
        read_dsh_session_via_bridge(request)
