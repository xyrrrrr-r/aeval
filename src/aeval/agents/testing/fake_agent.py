"""A second agent, in the smallest honest form: non-Node, ATIF-only.

Exists to test the framework's central claim — onboarding an agent costs one
adapter, not a core change. It deliberately has **no** Node runtime, **no** DSH
control stack and **no** DSH session file: everything it offers the chain is one
ATIF transcript plus one conversation session id. If a change to the core is
needed to accommodate an agent, this adapter is where it shows up first.

It is also the model for the conformance kit: what an adapter must declare
(``PROVIDES`` + the AdapterSpec fields) and what it must implement
(``name``/``version``/``paths``/``agent_session_id``/``read_trial_session``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeval.contracts import CanonicalTranscript, CompletenessRecord, FieldCompleteness, TranscriptCapability

REFERENCE_AGENT_IMPORT_PATH = "aeval.agents.testing.fake_agent:FakeAtifAgent"


@dataclass
class FakeAtifAgent:
    """Minimal conformant adapter: writes one ATIF transcript, nothing else."""

    PROVIDES = frozenset({"shell", "atif_native"})

    ADAPTER_ID = "fakeagent"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "installed_cli"
    # Honest: this adapter has no gateway integration, so its model traffic is
    # not metered. Such an adapter is refused for a suite that caps budget.
    BUDGET_ENFORCEMENT = "none"
    WRITE_SURFACE = "persistent"
    SERVER_SIDE_SESSION = "forbidden"
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        # one of the two values the literal allows; "an adapter with its own log
        # format that needs a converter" has no representation yet (todo)
        source="atif_native",
        reader="fake-atif-reader",
        capabilities=["atif_native"],
        fields_available={"events": "ok"},
    )

    name = "fakeagent"
    trial_dir: Path = field(default_factory=lambda: Path("."))
    observed_version: str = "0.0.1-fake"
    session_id_value: str = "fake-session-1"

    # This adapter has no DSH session file (by design), so its official
    # session record is its own ATIF document — the generic slot.
    SESSION_RECORD_OUTPUT = "agent_session_record"

    def version(self) -> str:
        return self.observed_version

    @property
    def agent_session_id(self) -> str | None:
        return self.session_id_value

    def paths(self) -> Any:
        logs_dir = Path(self.trial_dir) / "agent" / "logs"
        return type("FakePaths", (), {"logs_dir": logs_dir, "session_root": logs_dir})()

    def read_session_record(self) -> bytes:
        """Its official session record: the ATIF document itself."""
        return self.read_trial_session().atif.model_dump_json().encode("utf-8")

    def read_trial_session(self) -> CanonicalTranscript:
        from harbor.models.trajectories import Agent, Step, Trajectory

        atif = Trajectory(
            agent=Agent(name=self.name, version=self.version()),
            steps=[Step(step_id=1, source="agent", message="(fake agent did nothing)")],
        )
        return CanonicalTranscript(
            atif=atif,
            stop_reason="agent_exit_0",
            completeness=CompletenessRecord(
                fields=[
                    FieldCompleteness(field="events", status="ok"),
                    FieldCompleteness(field="token_usage", status="unavailable"),
                ]
            ),
        )
