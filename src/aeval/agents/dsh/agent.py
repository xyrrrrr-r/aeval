"""DshAgent — Harbor installed agent for DeepSeekHarness preview.

The adapter is marked experimental: DSH has no stable release; this
adapter is pinned to the exact 0.1.7-alpha.1 slice (plan §0.1) and
must never auto-upgrade.

Trajectory collection goes EXCLUSIVELY through the official
SessionPersistence read path (TS bridge); this class never parses
session files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent

from aeval.agents.dsh.atif_mapper import MAPPER_VERSION
from aeval.agents.dsh.bridge import (
    DshBridgeProtocolError,
    DshReaderFailure,
    DshReaderRequest,
    read_dsh_session_via_bridge,
)
from aeval.provenance import OFFICIAL_DSH_TAG

__all__ = ["DshAgent", "DshTrialSessionConfig"]


class DshTrialSessionConfig:
    """Per-trial isolation inputs for the DSH CLI inside the sandbox."""

    def __init__(
        self,
        *,
        trial_id: str,
        session_id: str,
        cwd: Path,
        session_root: Path,
        home: Path,
    ):
        self.trial_id = trial_id
        self.session_id = session_id
        self.cwd = cwd
        self.session_root = session_root
        self.home = home

    def env(self) -> dict[str, str]:
        """Per-trial environment: isolated cwd/HOME/session root (T1/T2)."""
        return {
            "DSH_SESSION_ROOT": str(self.session_root),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "TMPDIR": str(self.home / "tmp"),
        }


class DshAgent(BaseInstalledAgent):
    """Installed-agent adapter running the official DSH CLI in-sandbox."""

    MAPPER_VERSION = MAPPER_VERSION
    SUPPORTS_ATIF = False  # native sessions are converted via the bridge
    SUPPORTS_RESUME = False
    SUPPORTS_LOAD_NATIVE_TRAJECTORY = False
    SUPPORTS_LOAD_ATIF_TRAJECTORY = False
    SUPPORTS_CONFIG = False

    @property
    def name(self) -> str:  # type: ignore[override]
        return "dsh"

    @property
    def experimental(self) -> bool:
        return True

    @property
    def official_tag(self) -> str:
        return OFFICIAL_DSH_TAG

    async def install(self, environment: Any) -> None:
        """Install the pinned DSH CLI into the sandbox.

        The install is driven by the release lock (npm slice + Node
        version); any drift from the lock aborts the trial as
        infra_invalid rather than silently testing a different agent.
        """
        from aeval.provenance import build_official_dsh_lock

        lock = build_official_dsh_lock()
        if lock.official_tag != OFFICIAL_DSH_TAG:
            raise RuntimeError(
                f"DSH release lock drifted: expected {OFFICIAL_DSH_TAG}, "
                f"got {lock.official_tag}"
            )

    async def prepare_trial_session(self, trial_id: str) -> DshTrialSessionConfig:
        """Fresh, isolated session inputs for one trial."""
        import tempfile
        import uuid

        base = Path(tempfile.mkdtemp(prefix=f"aeval-dsh-{trial_id}-"))
        return DshTrialSessionConfig(
            trial_id=trial_id,
            session_id=str(uuid.uuid4()),
            cwd=base / "cwd",
            session_root=base / "sessions",
            home=base / "home",
        )

    def collect_native_trajectory(self, trial: Any, session_dir: Path) -> Path:
        """Locate the synced native session dir for the trial.

        Harbor syncs ``remote_session_logs_dir`` before verification;
        this only resolves the path — reading is the bridge's job.
        """
        if not session_dir.is_dir():
            raise DshReaderFailure(
                "SESSION_NOT_FOUND",
                f"synced DSH session directory missing: {session_dir}",
            )
        return session_dir

    def convert_trajectory(self, logs_dir: Path) -> Any:
        """Convert a downloaded native session dir to ATIF via the bridge.

        Overrides the BaseInstalledAgent hook so ATIF previews work the
        same way as collection: official reader only, no JSONL parsing.
        """
        import os

        allowed_base = Path(
            os.environ.get("AEVAL_DSH_ALLOWED_BASE", str(logs_dir.parent))
        )
        sessions = [
            p for p in logs_dir.iterdir() if p.is_file() and p.suffix in (".jsonl", ".zst")
        ] if logs_dir.is_dir() else []
        if not sessions:
            raise DshReaderFailure(
                "SESSION_NOT_FOUND",
                f"no native session file found under {logs_dir}",
            )
        session_id = sessions[0].name
        response = read_dsh_session_via_bridge(
            DshReaderRequest(
                session_id=session_id,
                bridge_path=Path(
                    os.environ.get(
                        "AEVAL_DSH_BRIDGE",
                        str(Path(__file__).parents[3] / "tools" / "dsh-session-reader" / "dist" / "main.js"),
                    )
                ),
                allowed_base=allowed_base,
                source_root=logs_dir,
            )
        )
        from aeval.agents.dsh.atif_mapper import convert_dsh_read_to_atif

        return convert_dsh_read_to_atif(response)
