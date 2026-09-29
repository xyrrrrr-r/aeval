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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from aeval.contracts import TranscriptCapability
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from aeval.agents.dsh.atif_mapper import (
    MAPPER_VERSION,
    build_canonical_transcript,
    convert_dsh_read_to_atif,
    count_pre_dispatch_auxiliary_rejections,
    derive_stop_reason,
    read_dispatched_auxiliary_calls,
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
    "find_session_record",
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


def find_session_record(source_root: Path, session_id: str) -> Path | None:
    """Locate the official session record under the session root.

    Environment verification on the real cluster showed the official
    backend nests sessions under a PROJECT directory derived from the
    working directory::

        <session_root>/--home-user--/session-<id>/session.v4.jsonl.zstd

    while an older/flat layout puts the session directly under the root.
    The official reader resolves this itself (it hands the root to the
    official persistence backend), so aeval must not assume one shape:
    both are accepted, and ambiguity (two records for one id) is
    reported rather than guessed.
    """
    source_root = Path(source_root)
    # Bounded breadth-first search: the record sits one project level under
    # a session ROOT (``<root>/--home-user--/<id>/``), while a descriptor's
    # ``session_root`` names the DSH HOME (``dsh-home``), putting the record
    # at ``<dsh-home>/sessions/--home-user--/<id>/``. Both are handled, and
    # the search never wanders further than a couple of levels.
    candidates: list[Path] = []
    frontier = [source_root]
    for _depth in range(3):
        next_frontier: list[Path] = []
        for current in frontier:
            direct = current / session_id / SESSION_RECORD_FILENAME
            if direct.is_file() and direct not in candidates:
                candidates.append(direct)
            try:
                children = sorted(p for p in current.iterdir() if p.is_dir())
            except OSError:
                continue
            next_frontier.extend(children)
        if candidates:
            break
        frontier = next_frontier
    if not candidates:
        return None
    if len(candidates) > 1:
        raise DshRunError(
            f"session {session_id} has {len(candidates)} official records "
            f"under {source_root} — refusing to guess which one is the trial's"
        )
    return candidates[0]


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
    program's own flag, so it follows ``--json``.

    Environment verification on the real sandbox established the exact
    semantics of that flag — ``dsh --profile headless --help`` says
    "adopt the persisted Session with this id; an unknown id is an
    error" — so it RESUMES an existing session and can never be used to
    make DSH create one under an owner-chosen id. A freshly minted
    trial id therefore has to exist in the store first; that is the
    sandbox-side control plugin's job, and :meth:`DshAgent.run` checks
    for it before starting instead of failing mid-run.
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

    # Capabilities this adapter offers, matched against a suite's
    # driver.require before any trial starts (aeval.agents.contract).
    # acp_stdio/sdk_jsonrpc: the channels DSH is driven through; shell and
    # file_tools: what a task can ask the agent to do; resume: the run adopts a
    # pre-minted session instead of starting a fresh one.
    PROVIDES = frozenset({"acp_stdio", "sdk_jsonrpc", "shell", "file_tools", "resume"})

    # Recorded adapter identity (aeval.contracts.AdapterSpec). Without these a
    # run cannot say which adapter produced its trials — a second agent would be
    # indistinguishable from this one in the store and in comparability.
    # Observed in the live sandbox and bound to the lock before grading: the
    # Node runtime the DSH release is pinned against. (DSH npm package identity is
    # checked separately when the lock is verified.)
    REQUIRED_OBSERVATIONS = ("node",)

    # Where the agent's own state lives inside the sandbox, and where its session
    # artifact lands in the bundle. Declared rather than assumed by the framework:
    # these were hardcoded for every agent, which quietly gave a second agent a
    # DSH home it knows nothing about. The values are exactly what was hardcoded.
    SANDBOX_HOME = "/logs/agent/dsh-home"
    SESSION_ARTIFACT_DIR = "dsh-home"

    ADAPTER_ID = "dsh"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "acp_stdio"
    # Availability, not a per-run guarantee: a given run can still be downgraded
    # to ``partial`` at grading time (e.g. an untrusted token counter, D48).
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        source="native_session_via_bridge",
        reader="dsh-official-session-reader",
        capabilities=["atif_via_bridge", "claim_check", "token_usage"],
        fields_available={"events": "ok", "token_usage": "ok"},
    )
    # Model traffic goes through the aeval gateway lease, so budget enforcement
    # is a real measurement rather than a wall-clock kill (D47 accounting).
    BUDGET_ENFORCEMENT = "gateway_lease"
    WRITE_SURFACE = "ephemeral_overlay"

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
        install_prefix: str | None = None,
        npm_cache: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        workspace_dir: str | None = None,
        **kwargs: Any,
    ):
        self._install_prefix = install_prefix
        self._npm_cache = npm_cache
        # owner-supplied run environment (e.g. NODE_EXTRA_CA_CERTS for a
        # broker whose TLS certificate is signed by a private CA)
        self._extra_env: dict[str, str] = dict(extra_env or {})
        # The task workspace the run must use. DSH records a session's
        # working directory and refuses to resume it elsewhere, so the
        # owner mints the session and starts the run in the SAME cwd
        # (found on the real chain: "session was recorded in /workspace,
        # not /home/user").
        self._workspace_dir_value = workspace_dir
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
    def agent_session_id(self) -> str | None:
        """Contract member: the agent's conversation session (see dsh_session_id)."""
        return self._session_id

    @property
    def dsh_session_id(self) -> str | None:
        """Deprecated alias for ``agent_session_id``; kept for older callers."""
        """The DSH conversation session the last run drove.

        Deliberately NOT named ``session_id``: Harbor's ``BaseAgent``
        owns that attribute and assigns its own sandbox identifier to it
        (``<trial_name>__agent``). Shadowing it with a read-only property
        made every real trial die with "property 'session_id' of
        'DshAgent' object has no setter" (found during environment
        verification) — the two ids mean different things and must not
        share a name.

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
        if self._install_prefix:
            return f"{self._install_prefix.rstrip('/')}/bin/dsh --version"
        return "dsh --version"

    def pin_session(self, session_id: str) -> None:
        """Adopt the owner-assigned session identity for the next run.

        ``--session-id`` resumes an EXISTING session (D15), so the owner
        mints that session first and pins it here; the run then adopts
        the trial's own identity instead of minting an unrelated one,
        which is also what the control plugin's identity injection is
        scoped to.
        """
        self._pinned_session_id = checked_session_id(session_id)

    def add_patch_file(self, path: Path | str) -> None:
        """Register one extra Cordis patch layer for the next run.

        The owner deploys the control stack into the sandbox and points
        the profile at it; the patch must be in place before ``run()``.
        """
        value = str(path)
        if value not in self._patch_files:
            self._patch_files.append(value)

    def set_workspace_dir(self, path: str) -> None:
        """Set the working directory for the sandboxed run (owner-supplied)."""
        self._workspace_dir_value = path

    def set_run_env(self, key: str, value: str) -> None:
        """Set one extra environment variable for the sandboxed run."""
        self._extra_env[key] = value

    def cli_bin_dir(self) -> str | None:
        """Directory to prepend to PATH for the installed CLI, if any."""
        if self._install_prefix:
            return f"{self._install_prefix.rstrip('/')}/bin"
        return None

    def parse_version(self, stdout: str) -> str:
        return stdout.strip()

    async def install(self, environment: BaseEnvironment) -> None:
        """Install the pinned DSH CLI and refuse any drift from the lock.

        The slice is installed verbatim from the registry; a version that
        differs from the lock aborts the trial as infra_invalid instead of
        silently grading a different agent.
        """
        locked = self._locked_version()
        # Environment verification (example-lab): the DSH dependency tree is
        # ~502 MB while the e2b sandbox root filesystem can be as small as
        # 737 MB total (~268 MB free), so the default global install fails
        # with ENOSPC. An operator can point the install prefix and the npm
        # cache at a roomier filesystem (e.g. /dev/shm tmpfs on this
        # cluster). Defaults are unchanged when neither is configured.
        prefix = f"--prefix {shlex.quote(self._install_prefix)} " if self._install_prefix else ""
        cache = (
            f"npm_config_cache={shlex.quote(self._npm_cache)} "
            if self._npm_cache
            else ""
        )
        if self._install_prefix:
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {shlex.quote(self._install_prefix)}",
            )
        await self.exec_as_root(
            environment,
            command=(
                f"{cache}npm install --global --no-audit --no-fund {prefix}"
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
        if self._pinned_session_id is not None:
            await self._require_resumable_session(environment, paths)
        # A per-invocation variable name keeps the instruction out of the
        # command string that the environment echoes into its own logs.
        task_var = f"AEVAL_DSH_TASK_{uuid.uuid4().hex}"
        command = build_headless_command(
            patch_files=self._patches_in_environment(),
            session_id=self._pinned_session_id,
            stream_path=paths.container_stream_path,
            task_env_var=task_var,
        )
        run_env = {**paths.env(), **self._extra_env, task_var: instruction}
        bin_dir = self.cli_bin_dir()
        if bin_dir is not None:
            # A prefixed install is not on the default PATH. The export
            # must happen INSIDE the shell: passing "dir:$PATH" as an
            # environment VALUE leaves $PATH unexpanded, which wiped the
            # sandbox PATH and made every builtin-less command (mkdir,
            # tee) fail with exit 127 (found on the real host).
            command = (
                f"export PATH={shlex.quote(bin_dir)}"
                ':"$PATH"; ' + command
            )
        result = await self.exec_as_agent(
            environment,
            command=command,
            env=run_env,
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

    async def _require_resumable_session(
        self, environment: BaseEnvironment, paths: DshTrialPaths
    ) -> None:
        """A pinned trial session must already exist in the sandbox store.

        ``--session-id`` resumes an existing Session and errors on an
        unknown id (verified against dsh 0.1.7-alpha.1 in the real
        sandbox). An evaluation trial pins the OWNER-assigned session id,
        so something must have created that session before the run — the
        sandbox-side control plugin. Failing here gives that diagnosis
        instead of a mid-run "session does not exist".
        """
        sessions_root = paths.container_session_root
        listing = await self.exec_as_agent(
            environment,
            command=f"find {shlex.quote(sessions_root.as_posix())} -maxdepth 2 "
            f"-name {shlex.quote(str(self._pinned_session_id))} -type d 2>/dev/null | head -1",
        )
        if not (listing.stdout or "").strip():
            raise DshRunError(
                f"pinned trial session {self._pinned_session_id!r} does not exist "
                f"under {sessions_root.as_posix()} — DSH's --session-id only "
                "resumes an existing session, so the owner-assigned id must be "
                "created first (sandbox-side control plugin); refusing to run "
                "with a session DSH would mint on its own"
            )

    def _patches_in_environment(self) -> list[str]:
        return list(self._patch_files)

    def _workspace_dir(self) -> str | None:
        return self._workspace_dir_value

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
            convert_dsh_read_to_atif(
                response,
                zero_token_auxiliary_rejections=count_pre_dispatch_auxiliary_rejections(
                    paths.logs_dir
                ),
                dispatched_auxiliary_calls=read_dispatched_auxiliary_calls(
                    paths.logs_dir
                ),
            ),
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
        record = find_session_record(source_root, self._session_id)
        if record is None:
            raise DshRunError(
                f"official session record for {self._session_id} not found "
                f"under {source_root} (agent log download incomplete — the "
                "record must land before the session can be read)"
            )
        session_dir = record.parent
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
        if self._session_id is None:
            # The run never announced a session (it failed). The trial
            # already carries that exception; raising a second error from
            # this best-effort backfill masked the real cause on the real
            # host, so the backfill simply does not happen.
            return
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
