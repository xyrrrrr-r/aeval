"""The core reader lifts the self-carried window without naming the agent.

The trajectory panel may compute occupancy from the sealed evidence alone, but
the reader lives in ``aeval.cli`` (core) and so must stay agent-neutral: it
reads the driver-agnostic ``agent.extra.contextWindow`` slot, never an
agent-namespaced key, and refuses to invent a window that is absent or malformed.
"""

from __future__ import annotations

from types import SimpleNamespace

from aeval.cli import _evidence_context_window


def _evidence(extra):
    agent = SimpleNamespace(extra=extra)
    atif = SimpleNamespace(agent=agent)
    transcript = SimpleNamespace(atif=atif)
    return SimpleNamespace(transcript=transcript)


def test_reads_the_neutral_context_window_slot():
    assert _evidence_context_window(_evidence({"contextWindow": 65536})) == 65536


def test_ignores_an_agent_namespaced_window():
    # A pre-neutral key must NOT be silently honoured — the contract is the
    # neutral slot, so a namespaced window reads as "not carried".
    assert _evidence_context_window(_evidence({"dsh": {"context_window": 65536}})) is None


def test_absent_or_malformed_window_is_not_invented():
    assert _evidence_context_window(_evidence(None)) is None
    assert _evidence_context_window(_evidence({})) is None
    assert _evidence_context_window(_evidence({"contextWindow": None})) is None
    assert _evidence_context_window(_evidence({"contextWindow": 0})) is None
    assert _evidence_context_window(_evidence({"contextWindow": "65536"})) is None
    assert _evidence_context_window(_evidence({"contextWindow": True})) is None


def test_missing_agent_block_is_unstated_not_an_error():
    # Evidence without a transcript/agent must degrade to "no window", never raise.
    assert _evidence_context_window(SimpleNamespace()) is None
