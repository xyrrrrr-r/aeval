"""P1-1: agent release locks, generalised without invalidating sealed evidence.

The lock is the run's identity. Generalising it is only safe if a lock recorded
before the change still digests to the value stored next to it — otherwise every
sealed bundle fails its own attestation and R7-R10 become unrecomputable. The
first test here is that invariant, stated as an executable copy of the historical
digest algorithm.
"""

from __future__ import annotations

from aeval.contracts import AgentReleaseLock, RuntimeLock, _digest
from aeval.provenance import build_runtime_lock
from aeval.agents.dsh.release import build_official_dsh_lock


def _dsh_run() -> RuntimeLock:
    """A lock as a dsh run records it: the adapter's hook contributed its pin."""
    return build_runtime_lock(release_locks={"dsh": build_official_dsh_lock()})


def _legacy_payload(lock: RuntimeLock) -> dict:
    """A lock as it was recorded before ``agents``/``control_dist``/``facade_dist``
    existed.

    None of those keys is present in a historical record — the fields did not
    exist — so the simulation must drop every one of them, or it stops being a
    copy of the historical bytes and starts passing for the wrong reason. Each
    new optional section has to be added here when it lands.
    """
    payload = lock.model_dump(mode="json")
    payload.pop("agents", None)
    payload.pop("control_dist", None)
    payload.pop("facade_dist", None)
    return payload


def _historical_digest(payload: dict) -> str:
    """The digest algorithm exactly as it was (no exclusion rules)."""
    return _digest({k: v for k, v in payload.items() if k != "created_at"})


def test_a_lock_recorded_before_the_generalisation_recomputes_identically():
    lock = _dsh_run()
    payload = _legacy_payload(lock)
    recorded = _historical_digest(payload)

    reloaded = RuntimeLock.model_validate(payload)
    assert reloaded.agents == {}
    assert reloaded.digest() == recorded
    # and the DSH release is still there, just read through the generic shape
    assert reloaded.agent_lock("dsh") is not None


def test_agent_releases_participate_in_the_digest():
    lock = build_runtime_lock()
    before = lock.digest()
    lock.agents["otheragent"] = AgentReleaseLock(id="otheragent", version="2.0.0")
    assert lock.digest() != before
    lock.agents["otheragent"] = AgentReleaseLock(id="otheragent", version="2.0.1")
    assert lock.digest() != before


def test_legacy_dsh_release_is_projected_into_the_generic_shape():
    lock = _dsh_run()
    projected = lock.agent_lock("dsh")
    assert projected is not None
    assert projected.id == "dsh"
    assert projected.runtime == "node"
    assert projected.version == lock.dsh.official_tag
    assert projected.runtime_versions == lock.dsh.node_versions
    assert projected.packages == lock.dsh.packages
    # DSH-specific facts travel in extra rather than becoming core fields
    assert "cordis_version" in projected.extra
    # an explicit generic entry wins over the legacy projection
    lock.agents["dsh"] = AgentReleaseLock(id="dsh", version="explicit")
    assert lock.agent_lock("dsh").version == "explicit"


def test_an_agent_without_a_pinned_release_can_produce_a_runtime_lock():
    """A lock without a DSH section used to be impossible to build."""
    other = build_runtime_lock()
    assert other.dsh is None
    assert other.agents == {}
    assert other.agent_locks() == {}

    # a generic-shape release lands in the agents section
    generic = build_runtime_lock(
        release_locks={"otheragent": AgentReleaseLock(id="otheragent", version="2.0.0")}
    )
    assert generic.dsh is None
    assert set(generic.agent_locks()) == {"otheragent"}

    # a DSH run still pins the official release, identically to before: the
    # adapter's hook routes the legacy-shape lock into the legacy section —
    # byte-identically to passing the same lock explicitly
    dsh_run = _dsh_run()
    assert dsh_run.dsh is not None
    assert dsh_run.agent_locks().keys() == {"dsh"}
    assert dsh_run.model_dump(exclude={"created_at"}) == build_runtime_lock(
        dsh=build_official_dsh_lock()
    ).model_dump(exclude={"created_at"})
    # and the generic section stays empty: the dsh digest must not move
    assert dsh_run.agents == {}
