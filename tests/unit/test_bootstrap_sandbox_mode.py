"""The suite's declared sandbox posture reaches the DSH run.

DSH probes for a bwrap/Landlock runner before it confines a shell command
and refuses the command when the image ships neither — measured on the
first M0 pilot as "no sandbox backend is usable on this host", which left
two of three Terminal-Bench tasks unanswerable while the file-tool-only
task passed. A suite therefore declares ``driver.sandbox_mode``, and the
control-stack bootstrap has to carry it into the run environment as
``DSH_PERMISSION_MODE`` (the CLI's own deployment override).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from aeval.contracts import TrialPaths
from aeval.control.bootstrap import deploy_control_stack


class _Agent:
    def __init__(self) -> None:
        self.run_env: dict[str, str] = {}
        self.patch_files: list[str] = []
        self.workspace: str | None = None
        self.session: str | None = None

    def cli_bin_dir(self) -> str:
        return "/dev/shm/dshpkg/bin"

    def add_patch_file(self, path: str) -> None:
        self.patch_files.append(path)

    def pin_session(self, session_id: str) -> None:
        self.session = session_id

    def set_workspace_dir(self, path: str) -> None:
        self.workspace = path

    def set_run_env(self, key: str, value: str) -> None:
        self.run_env[key] = value


class _Environment:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []

    async def exec(self, command: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(command)
        return SimpleNamespace(return_code=0, exit_code=0, stdout='{"ok":true}')

    async def upload_file(self, source: str, target: str) -> None:
        self.uploads.append((source, target))


def _paths() -> TrialPaths:
    return TrialPaths(
        sandbox_cwd="/app",
        dsh_home="/logs/agent/dsh-home",
        bundle_path="/tmp/bundle.tar",
        session_root="dsh-home",
        download_root="agent",
    )


@pytest.fixture()
def control_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("export const x = 1;\n", "utf-8")
    return dist


async def _deploy(tmp_path: Path, control_dist: Path, *, sandbox_mode: str | None):
    agent, environment = _Agent(), _Environment()
    await deploy_control_stack(
        environment=environment,
        agent=agent,
        paths=_paths(),
        config={"sessionId": "s-1", "jobTokenFile": "/dev/shm/aeval-job-token"},
        control_dist=control_dist,
        control_ca=None,
        trial_id="trial-1",
        sandbox_mode=sandbox_mode,
    )
    return agent, environment


async def test_declared_sandbox_mode_reaches_the_run_env(tmp_path, control_dist):
    agent, _ = await _deploy(tmp_path, control_dist, sandbox_mode="danger-full-access")
    assert agent.run_env["DSH_PERMISSION_MODE"] == "danger-full-access"
    # the session cwd still comes from the same declaration path
    assert agent.workspace == "/app"
    assert agent.patch_files


async def test_no_declaration_leaves_dshs_own_default(tmp_path, control_dist):
    """Undeclared suites keep DSH's workspace-write default: the bootstrap
    must not invent a posture of its own."""
    agent, _ = await _deploy(tmp_path, control_dist, sandbox_mode=None)
    assert "DSH_PERMISSION_MODE" not in agent.run_env
