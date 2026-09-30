"""Control options: a family's knobs stay the family's, end to end.

DSH probes for a bwrap/Landlock runner before it confines a shell command and
refuses the command when the image ships neither — measured on the first M0
pilot as "no sandbox backend is usable on this host", which left two of three
Terminal-Bench tasks unanswerable. The knob is DSH's own, so the suite declares
it under ``driver.control_options.dsh`` (not a framework field), the dispatcher
hands the dsh flavor exactly its own namespace, and the flavor translates it
into the run env. These tests pin all three links plus the fail-closed edges.
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


async def _deploy(tmp_path: Path, control_dist: Path, *, run_env=None):
    agent, environment = _Agent(), _Environment()
    await deploy_control_stack(
        environment=environment,
        agent=agent,
        paths=_paths(),
        config={"sessionId": "s-1", "jobTokenFile": "/dev/shm/aeval-job-token"},
        control_dist=control_dist,
        control_ca=None,
        trial_id="trial-1",
        run_env=run_env,
    )
    return agent, environment


# --- link 1: the framework is neutral ----------------------------------------


async def test_run_env_is_carried_into_the_agent_run_verbatim(tmp_path, control_dist):
    """The bootstrap sets whatever the deploying flavor names — the framework
    knows no family's variable."""
    agent, _ = await _deploy(
        tmp_path, control_dist, run_env={"ANY_FAMILY_KNOB": "on"}
    )
    assert agent.run_env["ANY_FAMILY_KNOB"] == "on"
    # the session cwd still comes from the same declaration path
    assert agent.workspace == "/app"
    assert agent.patch_files


async def test_no_run_env_leaves_the_agents_own_default(tmp_path, control_dist):
    """Undeclared options change nothing: the bootstrap must not invent one."""
    agent, _ = await _deploy(tmp_path, control_dist, run_env=None)
    assert agent.run_env == {}


# --- link 2: the dispatcher hands a flavor only its own namespace ------------


def test_the_dispatcher_resolves_a_flavors_own_namespace():
    from aeval.control.bootstrap import control_options_for

    context = SimpleNamespace(
        suite=SimpleNamespace(
            overlay=SimpleNamespace(
                driver=SimpleNamespace(
                    control_options={
                        "dsh": {"permission_mode": "danger-full-access"},
                        "other": {"knob": 1},
                    }
                )
            )
        )
    )
    assert control_options_for(context, "dsh") == {
        "permission_mode": "danger-full-access"
    }
    assert control_options_for(context, "unregistered") == {}
    # a suite with no options at all is not an error
    empty = SimpleNamespace(
        suite=SimpleNamespace(overlay=SimpleNamespace(driver=SimpleNamespace()))
    )
    assert control_options_for(empty, "dsh") == {}


# --- link 3: the dsh flavor validates and translates its own options ---------


def test_the_dsh_flavor_translates_its_permission_mode():
    from aeval.agents.dsh.control_flavor import _dsh_run_env

    assert _dsh_run_env(None) == {}
    assert _dsh_run_env({}) == {}
    assert _dsh_run_env({"permission_mode": "danger-full-access"}) == {
        "DSH_PERMISSION_MODE": "danger-full-access"
    }


def test_the_dsh_flavor_refuses_unknown_options_and_values():
    from aeval.agents.dsh.control_flavor import _dsh_run_env

    with pytest.raises(Exception, match="known options"):
        _dsh_run_env({"permission_mode": "read-only", "typo": 1})
    with pytest.raises(Exception, match="must be one of"):
        _dsh_run_env({"permission_mode": "yolo"})


async def test_the_facade_flavor_refuses_options_it_does_not_consume():
    """The other half of the rule: a stack that consumes nothing must not
    accept a knob silently — otherwise the suite declares a fact nobody reads."""
    from aeval.control.bootstrap import BootstrapError, _deploy_facade_flavor

    with pytest.raises(BootstrapError, match="consumes no control options"):
        await _deploy_facade_flavor(
            environment=None, context=None, agent=None, paths=None,
            config={}, trial_id="trial-1", control_dist=None, control_ca=None,
            facade_dist=None, control_options={"knob": 1},
        )
