"""DshAgent — Harbor installed agent for the official DeepSeek Harness CLI.

The run entry is the shipped ``headless`` profile (bundles ``dsh-base`` +
``dsh-headless``): ``dsh --profile headless --json`` takes the task on
stdin, announces ``{"type": "session", "sessionId": …}`` as its first
stdout event, and persists the session under ``$DSH_HOME/sessions``.

Trajectory collection goes EXCLUSIVELY through the official
SessionPersistence read path (the compiled ``dsh-eval-control`` session
reader); this class never parses session files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from aeval.agents.dsh.atif_mapper import (
    MAPPER_VERSION,
    build_canonical_transcript,
    convert_dsh_read_to_atif,
    derive_stop_reason,
)
from aeval.agents.dsh.bridge import (
    DshReaderRequest,
    read_dsh_session_via_bridge,
)
from aeval.contracts import CanonicalTranscript
from aeval.provenance import OFFICIAL_DSH_TAG

__all__ = [
    "DshAgent",
    "DshRunError",
    "DshTrialPaths",
    "HEADLESS_PROFILE",
    "host_session_root",
    "resolve_session_reader",
    "session_id_from_stream",
    "session_reader_candidates",
]

# The shipped one-shot profile, i.e. ``dsh-app-boot``'s PROFILE_TEMPLATES entry
# ``headless: [dsh-base, dsh-headless]``.
HEADLESS_PROFILE = "headless"

# ``dsh-base`` mounts session-persistence-jsonl at ``dshHomePath('sessions')``,
# so the session root is always one ``sessions`` child of the harness home.
DSH_HOME_ENV = "DSH_HOME"
DSH_HOME_DIRNAME = "dsh-home"
SESSIONS_DIRNAME = "sessions"

# The ``--json`` run stream is tee'd here so the durable projection of the run
# survives in the synced trial logs next to the session it describes.
RUN_STREAM_FILENAME = "dsh-run.jsonl"

# The official backend persists exactly one session record per session
# directory, with this name (session reader contract, dsh-eval-control).
SESSION_RECORD_FILENAME = "session.v4.jsonl.zstd"

# ``dsh`` announces its own ids as ``session-<uuid>``; a pinned trial id has to
# survive both a shell argument and the on-disk session directory name.
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# The upstream API key never enters the sandbox; the reader is host-side.
SESSION_READER_ENV = "AEVAL_DSH_SESSION_READER"
SESSION_READER_ROOT_ENV = "AEVAL_DSH_CONTROL_ROOT"
_ALLOWED_BASE_ENV = "AEVAL_DSH_ALLOWED_BASE"

_DEFAULT_NODE_PACKAGE = "@deepseek-ai/dsh"


class DshRunError(RuntimeError):
    """The DSH run or its collection failed → trial is infra_invalid."""


@dataclass(frozen=True)
class DshTrialPaths:
    """One trial's DSH home, in the sandbox view and in the synced host view."""

    environment_logs_dir: PurePosixPath
    logs_dir: Path

    @property
    def dsh_home(self) -> PurePosixPath:
        return self.environment_logs_dir / DSH_HOME_DIRNAME

    @property
    def container_session_root(self) -> PurePosixPath:
        return self.dsh_home / SESSIONS_DIRNAME

    @property
    def container_stream_path(self) -> PurePosixPath:
        return self.environment_logs_dir / RUN_STREAM_FILENAME

    def env(self) -> dict[str, str]:
        return {DSH_HOME_ENV: self.dsh_home.as_posix()}

    def as_dict(self) -> dict[str, str]:
        return {
            "dsh_home": self.dsh_home.as_posix(),
            "session_root": self.container_session_root.as_posix(),
            "run_stream": self.container_stream_path.as_posix(),
        }


def host_session_root(logs_dir: Path) -> Path:
    """The synced host copy of one trial's official session root."""
    return logs_dir / DSH_HOME_DIRNAME / SESSIONS_DIRNAME


def checked_session_id(value: str) -> str:
    """Return a usable trial session id, or fail before the sandbox is touched.

    The id is both a shell argument and the on-disk session directory name
    the official reader resolves, so anything looser than a bare identifier
    is rejected here rather than at collection time.
    """
    if not _SESSION_ID_PATTERN.fullmatch(value):
        raise DshRunError(
            f"invalid DSH trial session id: {value!r}; expected 1-128 "
            "characters of [A-Za-z0-9._-] starting with a letter or digit"
        )
    return value


def session_id_from_stream(stdout: str) -> str:
    """Return the session id the headless runner announced first.

    A fresh headless run mints its own session identity, so the harness
    cannot know it in advance; ``--session-id`` instead adopts a session
    that already exists in the store, which is how a trial with a control
    plugin pins its own identity. Everything before the announcement is
    launcher noise.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "session":
            continue
        session_id = event.get("sessionId")
        if isinstance(session_id, str) and session_id:
            return session_id
        raise DshRunError("headless session event carried no sessionId")
    raise DshRunError("headless run stream carried no opening session event")


def build_headless_command(
    *,
    patch_files: Iterable[str] = (),
    session_id: str | None = None,
    stream_path: PurePosixPath,
    task_env_var: str,
) -> str:
    """Render the one-shot command that boots DSH inside the sandbox.

    The task arrives through an environment variable rather than a
    positional argument so the instruction never lands in the process list
    or in the shell history captured by the environment logs.

    Overlay patches go before ``--json``: the launcher parses only its own
    flags and forwards everything from the first unrecognised token on to
    the booted profile verbatim, so a later ``--patch`` would reach the
    headless command program as an unknown option. ``--session-id`` is that
    program's own flag, so it follows ``--json``; it adopts a session that
    already exists in the store rather than minting one.
    """
    parts = ["dsh", "--profile", HEADLESS_PROFILE]
    for patch in patch_files:
        parts += ["--patch", shlex.quote(patch)]
    parts.append("--json")
    if session_id is not None:
        parts += ["--session-id", shlex.quote(session_id)]
    quoted_stream = shlex.quote(stream_path.as_posix())
    return (
        f"mkdir -p {shlex.quote(stream_path.parent.as_posix())} && "
        f"printf '%s' \"${{{task_env_var}}}\" | "
        f"{' '.join(parts)} | tee {quoted_stream}"
    )


def session_reader_candidates() -> list[Path]:
    """Built readers to try when the operator has not named one.

    ``dsh-eval-control`` is a sibling checkout in the development layout and
    an npm dependency in an installed one; both are probed, in that order.
    """
    repo_dir = Path(__file__).parents[4]
    candidates = [
        repo_dir.parent / "dsh-eval-control" / "dist" / "session_reader.js"
    ]
    root_env = os.environ.get(SESSION_READER_ROOT_ENV)
    if root_env:
        candidates.insert(0, Path(root_env) / "dist" / "session_reader.js")
    candidates.append(
        repo_dir / "node_modules" / "dsh-eval-control" / "dist" / "session_reader.js"
    )
    return candidates


def resolve_session_reader(explicit: Path | None = None) -> Path:
    """Locate the compiled official session reader of ``dsh-eval-control``.

    The reader is the ``aeval-dsh-session-reader`` bin of the frozen
    TypeScript package. A configured path is authoritative: silently reading
    a trial's session with some other script would defeat the point of
    reading it through the official backend at all.
    """
    override = explicit or (
        Path(value) if (value := os.environ.get(SESSION_READER_ENV)) else None
    )
    if override is not None:
        if not override.is_file():
            raise DshRunError(
                f"configured DSH session reader not found: {override} "
                "(build dsh-eval-control or unset "
                f"{SESSION_READER_ENV})"
            )
        return override
    candidates = session_reader_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DshRunError(
        "official DSH session reader not found; tried "
        + ", ".join(str(c) for c in candidates)
        + f". Build dsh-eval-control and point {SESSION_READER_ENV} at "
        "dist/session_reader.js."
    )


class DshAgent(BaseInstalledAgent):
    """Installed-agent adapter running the official DSH CLI in-sandbox."""

    MAPPER_VERSION = MAPPER_VERSION

    capabilities = AgentCapabilities(
        atif=False,  # ATIF comes from the official session read, not from the CLI
        resume=False,
        load_native_trajectory=False,
        load_atif_trajectory=False,
        handoff=False,
        native_config=False,
        windows=False,  # verified only on Linux task containers
    )

    _CLI_PACKAGE: ClassVar[str] = _DEFAULT_NODE_PACKAGE

    def __init__(
        self,
        *args: Any,
        session_reader: Path | str | None = None,
        patch_files: Iterable[Path | str] = (),
        session_id: str | None = None,
        run_timeout_sec: int | None = None,
        **kwargs: Any,
    ):
        self._session_reader = Path(session_reader) if session_reader else None
        self._patch_files = [str(p) for p in patch_files]
        self._pinned_session_id = (
            checked_session_id(session_id) if session_id is not None else None
        )
        self._run_timeout_sec = run_timeout_sec
        self._session_id: str | None = None
        self._transcript: CanonicalTranscript | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "dsh"

    def version(self) -> str | None:
        return self._version or self._locked_version()

    @property
    def experimental(self) -> bool:
        return True

    @property
    def official_tag(self) -> str:
        return OFFICIAL_DSH_TAG

    @property
    def session_id(self) -> str | None:
        """The session the last run drove, as its own stream announced it.

        With a pinned trial identity this is the id the run was told to
        adopt, attested by the runner announcing it back.
        """
        return self._session_id

    def paths(self) -> DshTrialPaths:
        return DshTrialPaths(
            environment_logs_dir=self.environment_logs_dir,
            logs_dir=self.logs_dir,
        )

    @staticmethod
    def _locked_version() -> str:
        from aeval.provenance import build_official_dsh_lock

        lock = build_official_dsh_lock()
        if lock.official_tag != OFFICIAL_DSH_TAG:
            raise DshRunError(
                f"DSH release lock drifted: expected {OFFICIAL_DSH_TAG}, "
                f"got {lock.official_tag}"
            )
        for package in lock.packages:
            if package.name == _DEFAULT_NODE_PACKAGE:
                return package.version
        raise DshRunError(f"DSH release lock does not pin {_DEFAULT_NODE_PACKAGE}")

    def get_version_command(self) -> str:
        return "dsh --version"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip()

    async def install(self, environment: BaseEnvironment) -> None:
        """Install the pinned DSH CLI and refuse any drift from the lock.

        The slice is installed verbatim from the registry; a version that
        differs from the lock aborts the trial as infra_invalid instead of
        silently grading a different agent.
        """
        locked = self._locked_version()
        await self.exec_as_root(
            environment,
            command=(
                "npm install --global --no-audit --no-fund "
                f"{shlex.quote(f'{self._CLI_PACKAGE}@{locked}')}"
            ),
        )
        result = await self.exec_as_agent(environment, command=self.get_version_command())
        installed = self.parse_version(result.stdout or "")
        if installed != locked:
            raise DshRunError(
                f"DSH CLI version drift: lock says {locked}, environment reports "
                f"{installed or 'nothing'}"
            )
        self._version = locked

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        paths = self.paths()
        self._session_id = None
        self._transcript = None
        # A per-invocation variable name keeps the instruction out of the
        # command string that the environment echoes into its own logs.
        task_var = f"AEVAL_DSH_TASK_{uuid.uuid4().hex}"
        command = build_headless_command(
            patch_files=self._patches_in_environment(),
            session_id=self._pinned_session_id,
            stream_path=paths.container_stream_path,
            task_env_var=task_var,
        )
        result = await self.exec_as_agent(
            environment,
            command=command,
            env={**paths.env(), task_var: instruction},
            cwd=self._workspace_dir(),
            timeout_sec=self._run_timeout_sec,
        )
        announced = session_id_from_stream(result.stdout or "")
        if (
            self._pinned_session_id is not None
            and announced != self._pinned_session_id
        ):
            raise DshRunError(
                "DSH drove a different session than the trial pinned: expected "
                f"{self._pinned_session_id}, run stream announced {announced}"
            )
        self._session_id = announced

    def _patches_in_environment(self) -> list[str]:
        return list(self._patch_files)

    def _workspace_dir(self) -> str | None:
        return None

    def read_trial_session(self) -> CanonicalTranscript:
        """Rebuild the canonical transcript from the synced official session.

        Reads through the official persistence backend only, once, and caches
        the result so grading and reporting cannot disagree about the same
        trial. Missing sync or missing session fails closed.
        """
        if self._transcript is not None:
            return self._transcript
        if self._session_id is None:
            raise DshRunError(
                "no session id recorded for this trial; run() must complete first"
            )
        paths = self.paths()
        source_root = host_session_root(paths.logs_dir)
        if not source_root.is_dir():
            raise DshRunError(
                f"synced DSH session root missing: {source_root} "
                "(check that the trial synced its agent logs)"
            )
        self._verify_download_complete(source_root)
        allowed_base_env = os.environ.get(_ALLOWED_BASE_ENV)
        response = read_dsh_session_via_bridge(
            DshReaderRequest(
                session_id=self._session_id,
                bridge_path=resolve_session_reader(self._session_reader),
                allowed_base=(
                    Path(allowed_base_env) if allowed_base_env else paths.logs_dir
                ),
                source_root=source_root,
            )
        )
        self._transcript = build_canonical_transcript(
            convert_dsh_read_to_atif(response),
            evidence=None,
            stop_reason=derive_stop_reason(response.events, response.inherited_event_count),
        )
        return self._transcript

    def _verify_download_complete(self, source_root: Path) -> None:
        """The synced session must be the official layout, complete (P0-5).

        A partially synced or absent session directory is a download
        failure: reading through it would silently grade a truncated
        trajectory. The official backend persists exactly one session
        record (``session.v4.jsonl.zstd``) per session directory, plus
        an optional empty POSIX ``session.lock`` lease artifact.
        """
        session_dir = source_root / self._session_id
        if not session_dir.is_dir():
            raise DshRunError(
                f"synced session directory missing: {session_dir} "
                "(agent log download incomplete — refusing to read a "
                "session that was never synced)"
            )
        record = session_dir / SESSION_RECORD_FILENAME
        if not record.is_file():
            raise DshRunError(
                f"session record missing in synced session: {record} "
                "(agent log download incomplete — the official record "
                "must land before the session can be read)"
            )
        records = [
            p for p in session_dir.iterdir()
            if p.is_file() and p.name.startswith("session.v") and p.suffix == ".zstd"
        ]
        if len(records) != 1:
            raise DshRunError(
                f"session directory holds {len(records)} session records, "
                f"expected exactly one: {session_dir}"
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Backfill Harbor's usage totals and DSH session pointers.

        Runs only while the AgentContext is still empty (Harbor's
        ``_populate_agent_context`` skips otherwise) — which is exactly
        why ``run()`` must NOT write ``context.metadata``: doing so made
        the context non-empty and silently skipped this backfill
        (P0-5 fix, the reader callback was never invoked).

        Numbers come from the durable session, never from the run stream, so
        a claim the agent cannot support cannot inflate the recorded cost.
        """
        paths = self.paths()
        context.metadata = {
            **(context.metadata or {}),
            "dsh_session_id": self._session_id,
            "dsh_run_stream": paths.container_stream_path.as_posix(),
            "dsh_home": paths.dsh_home.as_posix(),
        }
        metrics = self.read_trial_session().atif.final_metrics
        if metrics is None or metrics.total_prompt_tokens is None:
            return
        context.n_input_tokens = metrics.total_prompt_tokens
        context.n_output_tokens = metrics.total_completion_tokens
        context.n_cache_tokens = metrics.total_cached_tokens
