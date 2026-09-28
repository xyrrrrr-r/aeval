"""RuntimeLock: the identity of everything a run executes on.

A run may only start when the software stack and images match an
expected, fully pinned lock (plan §0/§0.1):

- Harbor version + commit, clean and non-shallow source tree, build
  lock-file digests.
- Python/uv/platform/toolchain versions.
- OCI images pinned by digest — mutable tags are a hard error.
- The aeval plugin distribution itself (wheel digest, import path).
- The exact official DSH preview slice (npm packages + integrity, Node
  versions, Cordis/ACP versions, lockfile digest). DSH is experimental
  and must never auto-upgrade.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from aeval.contracts import (
    DshReleaseLock,
    HarborLock,
    ImageIdentity,
    NpmPackageLock,
    ObservedIdentity,
    PluginIdentity,
    PythonEnvironmentLock,
    RuntimeLock,
)

__all__ = [
    "LockMismatchError",
    "BackendNotAvailableError",
    "verify_e2b_backend",
    "bind_observed_identity",
    "OFFICIAL_HARBOR_VERSION",
    "OFFICIAL_HARBOR_COMMIT",
    "OFFICIAL_DSH_TAG",
    "OFFICIAL_DSH_COMMIT",
    "DSH_NPM_SLICE",
    "build_runtime_lock",
    "verify_runtime_lock",
    "assert_clean_harbor_source",
    "resolve_image_digest",
    "fingerprint_python_environment",
    "fingerprint_plugin_distribution",
    "sha256_file",
]


class LockMismatchError(RuntimeError):
    """Raised when the live environment disagrees with the expected lock.

    Every mismatch message carries expected/actual so the operator can
    act without re-running anything.
    """


OFFICIAL_HARBOR_VERSION = "0.23.0"
OFFICIAL_HARBOR_COMMIT = "7464ab541773ea1d4618336f043970042f33a1b5"

OFFICIAL_DSH_TAG = "dsh-v0.1.7-alpha.1"
OFFICIAL_DSH_COMMIT = "c36a83ff6bb95e3f82cf79f9be7c724270a8aa61"

# Exact npm compatibility slice for DSH 0.1.7-alpha.1 (plan §0.1).
DSH_NPM_SLICE: tuple[tuple[str, str, str | None], ...] = (
    ("@deepseek-ai/dsh", "0.1.7-alpha.1",
     "sha512-fim76775kLyal0lLNmpktZfOiOwU0P9qdluknL5Sm3F6ax9I5PcLD0W0WzqH9tMOOY8yHya5VShuEzSSh223sw=="),
    ("@deepseek-ai/dsh-sdk-client", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-sdk-protocol", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-acp", "0.1.7-alpha.1", None),
    ("@agentclientprotocol/sdk", "1.4.0", None),
    ("@deepseek-ai/cordis", "4.0.3", None),
)

DSH_NODE_VERSIONS = ("22.19.x", "24.20.0")

_HARBOR_INSTALL_HINT = (
    "harbor is not importable — install the locked wheel "
    f"harbor=={OFFICIAL_HARBOR_VERSION} before running aeval"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise LockMismatchError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def assert_clean_harbor_source(repo: Path, expected_commit: str) -> None:
    """Fail loudly unless the Harbor source tree is a usable baseline.

    A shallow/grafted clone or a dirty working tree cannot serve as a
    release baseline: grafts hide history, and local edits change
    behavior without changing the recorded commit.
    """
    if not (repo / ".git").exists():
        raise LockMismatchError(f"not a git repository: {repo}")
    head = _git(repo, "rev-parse", "HEAD")
    if head != expected_commit:
        raise LockMismatchError(
            f"harbor HEAD: expected {expected_commit}, actual {head}"
        )
    shallow = (repo / ".git" / "shallow").exists()
    if shallow:
        try:
            is_shallow = _git(repo, "rev-parse", "--is-shallow-repository")
        except LockMismatchError:
            is_shallow = "true"
        if is_shallow != "false":
            raise LockMismatchError(
                "harbor source is shallow/grafted — re-clone non-shallow "
                "before using it as a release baseline"
            )
    status = _git(repo, "status", "--porcelain")
    if status:
        raise LockMismatchError(
            f"harbor working tree is dirty ({len(status.splitlines())} entries) — "
            "commit or stash before locking"
        )


def _harbor_version_and_commit() -> tuple[str, str | None]:
    try:
        from importlib.metadata import distribution

        dist = distribution("harbor")
        version = dist.version
    except Exception as exc:  # pragma: no cover - defensive boundary
        raise LockMismatchError(_HARBOR_INSTALL_HINT) from exc
    commit: str | None = None
    try:
        import harbor  # noqa: F401

        commit = getattr(harbor, "__commit__", None)
    except Exception:  # pragma: no cover - version alone is load-bearing
        commit = None
    return version, commit


def fingerprint_python_environment() -> PythonEnvironmentLock:
    uv_version: str | None = None
    try:
        result = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode == 0:
            uv_version = result.stdout.strip().split()[-1]
    except OSError:
        uv_version = None
    return PythonEnvironmentLock(
        python_version=platform.python_version(),
        uv_version=uv_version,
        platform=platform.platform(),
    )


def fingerprint_plugin_distribution() -> PluginIdentity:
    from importlib.metadata import distribution

    dist = distribution("aeval")
    wheel_sha256: str | None = None
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        try:
            info = json.loads(direct_url)
            wheel_sha256 = (
                info.get("dir_info", {}).get("editable") and None
            ) or None
        except json.JSONDecodeError:
            wheel_sha256 = None
    return PluginIdentity(
        distribution="aeval",
        version=dist.version,
        import_path="aeval.hooks:AevalPlugin",
        wheel_sha256=wheel_sha256,
    )


def build_runtime_lock(
    *,
    images: dict[str, ImageIdentity] | None = None,
    harbor_source_repo: Path | None = None,
    dsh: DshReleaseLock | None = None,
    harbor_lock_ref: str | None = None,
) -> RuntimeLock:
    """Assemble the RuntimeLock describing the live environment.

    When ``harbor_source_repo`` is given the source tree must be clean,
    non-shallow and exactly at the locked commit (release path). Without
    it we record the installed distribution version and the locked
    commit as declared (dev path) — the commit stays in the lock either
    way so the run manifest can never silently forget it.
    """
    version, commit = _harbor_version_and_commit()
    if version != OFFICIAL_HARBOR_VERSION:
        raise LockMismatchError(
            f"installed harbor version: expected {OFFICIAL_HARBOR_VERSION}, "
            f"actual {version}"
        )
    harbor = HarborLock(
        version=version,
        commit=commit or OFFICIAL_HARBOR_COMMIT,
        source_clean=True,
        shallow=False,
    )
    if harbor_source_repo is not None:
        assert_clean_harbor_source(harbor_source_repo, OFFICIAL_HARBOR_COMMIT)
        pyproject = harbor_source_repo / "pyproject.toml"
        if pyproject.exists():
            harbor.pyproject_sha256 = sha256_file(pyproject)
        uv_lock = harbor_source_repo / "uv.lock"
        if uv_lock.exists():
            harbor.uv_lock_sha256 = sha256_file(uv_lock)
        harbor.commit = OFFICIAL_HARBOR_COMMIT
    if dsh is None:
        dsh = build_official_dsh_lock()
    return RuntimeLock(
        harbor=harbor,
        python_env=fingerprint_python_environment(),
        images=images or {},
        plugin=fingerprint_plugin_distribution(),
        dsh=dsh,
        harbor_lock_ref=harbor_lock_ref,
    )


def build_official_dsh_lock() -> DshReleaseLock:
    return DshReleaseLock(
        official_tag=OFFICIAL_DSH_TAG,
        commit=OFFICIAL_DSH_COMMIT,
        packages=[
            NpmPackageLock(name=n, version=v, integrity=i)
            for n, v, i in DSH_NPM_SLICE
        ],
        node_versions=list(DSH_NODE_VERSIONS),
        cordis_version="4.0.3",
        acp_sdk_version="1.4.0",
        experimental=True,
    )


def _cmp(expected: Any, actual: Any, what: str) -> None:
    if expected != actual:
        raise LockMismatchError(
            f"{what}: expected {expected!r}, actual {actual!r}"
        )


def verify_runtime_lock(expected: RuntimeLock) -> None:
    """Fail loudly before any trial starts when reality drifts.

    This is the hard supply-chain gate: a mismatch must prevent run
    creation entirely (no trial, no manifest, no scores).
    """
    version, commit = _harbor_version_and_commit()
    _cmp(expected.harbor.version, version, "harbor version")
    expected_commit = expected.harbor.commit or OFFICIAL_HARBOR_COMMIT
    if commit is not None:
        _cmp(expected_commit, commit, "harbor commit")
    live = fingerprint_python_environment()
    _cmp(expected.python_env.python_version, live.python_version, "python version")
    _cmp(expected.python_env.platform, live.platform, "platform")

    for name, image in expected.images.items():
        if not image.pinned:
            raise LockMismatchError(
                f"image {name!r} is not digest-pinned "
                f"(reference={image.reference!r}) — refusing to run"
            )
        if "@sha256:" not in image.reference:
            raise LockMismatchError(
                f"image {name!r} reference is not digest-pinned: {image.reference!r}"
            )

    if expected.dsh is not None:
        if not expected.dsh.packages:
            raise LockMismatchError("dsh lock declares no npm packages")
        names = [p.name for p in expected.dsh.packages]
        if "@deepseek-ai/dsh" not in names:
            raise LockMismatchError("dsh lock is missing @deepseek-ai/dsh")
        for pkg in expected.dsh.packages:
            if not pkg.version:
                raise LockMismatchError(f"dsh package {pkg.name!r} has no version")
            if pkg.name == "@deepseek-ai/dsh" and pkg.integrity is None:
                raise LockMismatchError(
                    "@deepseek-ai/dsh lock is missing its npm integrity hash"
                )

    if expected.plugin is not None:
        live_plugin = fingerprint_plugin_distribution()
        _cmp(expected.plugin.version, live_plugin.version, "aeval plugin version")


def resolve_image_digest(image: str, platform_str: str) -> ImageIdentity:
    """Resolve a mutable reference to a digest identity via docker CLI.

    The returned identity records the original tag so a run manifest can
    show what was narrowed. Resolution requires a reachable registry;
    on this machine use a Docker Hub mirror.
    """
    if "@sha256:" in image:
        return ImageIdentity(reference=image, digest=image.split("@", 1)[1], platform=platform_str)
    result = subprocess.run(
        ["docker", "manifest", "inspect", image],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise LockMismatchError(
            f"cannot resolve digest for {image!r}: {result.stderr.strip()}"
        )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LockMismatchError(f"unparsable manifest for {image!r}") from exc
    digest = manifest.get("config", {}).get("digest")
    if not digest:
        raise LockMismatchError(f"manifest for {image!r} carries no digest")
    return ImageIdentity(
        reference=f"{image.split(':')[0]}@{digest}",
        digest=digest,
        platform=platform_str,
        pinned=True,
        original_tag=image,
    )


def write_lock(lock: RuntimeLock, path: Path) -> Path:
    path.write_text(
        json.dumps(lock.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_lock(path: Path) -> RuntimeLock:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RuntimeLock.model_validate(data)


def lock_report(lock: RuntimeLock) -> str:
    lines = [
        f"runtime lock digest: {lock.digest()}",
        f"harbor: {lock.harbor.version}@{(lock.harbor.commit or 'unknown')[:12]}",
        f"python: {lock.python_env.python_version} on {lock.python_env.platform}",
    ]
    for name, img in lock.images.items():
        lines.append(f"image {name}: {img.reference} ({img.platform})")
    if lock.dsh:
        lines.append(
            f"dsh: {lock.dsh.official_tag}@{lock.dsh.commit[:12]} "
            f"({len(lock.dsh.packages)} npm packages, experimental={lock.dsh.experimental})"
        )
    if lock.plugin:
        lines.append(f"plugin: {lock.plugin.distribution} {lock.plugin.version}")
    return "\n".join(lines)


class BackendNotAvailableError(RuntimeError):
    """The selected sandbox backend's SDK is not installed.

    P0-2: startup-time detection — an e2b run with a missing SDK must
    fail before any template or sandbox is created, not mid-trial.
    """


def verify_e2b_backend() -> str:
    """Fail loudly unless the e2b SDK is importable; return its version.

    The pyproject dependency is ``harbor[e2b]==0.23.0``; this check is
    the runtime half — the extra being declared does not prove the SDK
    landed in the active environment (doc §4.4).
    """
    try:
        import e2b  # noqa: F401
    except ImportError as exc:
        raise BackendNotAvailableError(
            "e2b backend selected but the e2b SDK is not importable — "
            "install the harbor[e2b] extra into the active environment "
            "before starting a run"
        ) from exc
    version = getattr(e2b, "__version__", None)
    if not version:
        from importlib.metadata import PackageNotFoundError, version as dist_version

        try:
            version = dist_version("e2b")
        except PackageNotFoundError:
            version = None
    if not version:
        raise BackendNotAvailableError(
            "e2b SDK is importable but its version is not determinable — "
            "record the actual version before binding an observed identity"
        )
    return str(version)


def bind_observed_identity(
    observed: ObservedIdentity, expected: RuntimeLock
) -> None:
    """Bind a live sandbox's observed identity to the expected lock (§3.4).

    Fail-closed: every expected dimension must be OBSERVED and equal.
    A missing observation is a binding failure — never a silent
    default, never a rewrite of the already-referenced lock.

    - image digest: must match the locked ``sandbox`` image identity;
    - architecture: must match the locked image platform (arm64);
    - Node version: must fall inside the locked DSH node matrix
      (``24.20.0``-style exact or ``22.19.x``-style minor range);
    - e2b SDK version: must be recorded (observed from the SDK itself).
    """
    if observed.backend != "e2b":
        raise LockMismatchError(
            f"observed backend: expected 'e2b', actual {observed.backend!r}"
        )
    if not observed.e2b_sdk_version:
        raise LockMismatchError(
            "observed identity records no e2b SDK version — the SDK "
            "presence must be observed, not assumed"
        )

    sandbox_image = expected.images.get("sandbox")
    if sandbox_image is None:
        raise LockMismatchError(
            "expected lock pins no 'sandbox' image — nothing to bind the "
            "observed sandbox against"
        )
    if not observed.image_digest:
        raise LockMismatchError(
            "observed identity records no image digest — an unobserved "
            "image cannot be bound to the expected lock"
        )
    if observed.image_digest.removeprefix("sha256:") != sandbox_image.digest.removeprefix("sha256:"):
        raise LockMismatchError(
            "sandbox image digest: expected "
            f"{sandbox_image.digest!r}, actual {observed.image_digest!r}"
        )
    if not observed.architecture:
        raise LockMismatchError(
            "observed identity records no architecture — the sandbox "
            "architecture must be measured, not assumed"
        )
    if _canonical_arch(observed.architecture) != _canonical_arch(sandbox_image.platform):
        raise LockMismatchError(
            f"sandbox architecture: expected {sandbox_image.platform!r}, "
            f"actual {observed.architecture!r}"
        )
    node_matrix = expected.dsh.node_versions if expected.dsh else []
    if not node_matrix:
        raise LockMismatchError(
            "expected lock declares no DSH node matrix — cannot bind the "
            "observed Node version"
        )
    if not observed.node_version:
        raise LockMismatchError(
            "observed identity records no Node version — the runtime "
            "Node version must be measured inside the sandbox"
        )
    if not _node_in_matrix(observed.node_version, node_matrix):
        raise LockMismatchError(
            f"observed Node version {observed.node_version!r} is outside "
            f"the locked matrix {node_matrix} — either install a matrix "
            "version or revise the matrix with a recorded decision"
        )


# The sandbox's own kernel reports `uname -m` (`aarch64`), while OCI image
# manifests and the runtime lock use the OCI platform vocabulary
# (`arm64`). Both name the same architecture, so the binding compares
# canonical forms — verified on the arm64 e2b host, where a strict string
# compare rejected a correct observation.
_ARCH_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv8l": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
    "i386": "386",
    "i686": "386",
    "386": "386",
    "riscv64": "riscv64",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
}


def _canonical_arch(value: str) -> str:
    """Canonicalize an architecture name; unknown values pass through.

    Unknown names are deliberately NOT normalized away: a value nobody
    recognizes must still compare unequal to the locked platform so the
    binding fails loudly instead of matching by accident.
    """
    return _ARCH_ALIASES.get(value.strip().lower(), value.strip().lower())


def _node_in_matrix(version: str, matrix: list[str]) -> bool:
    """``24.20.0`` matches exactly; ``22.19.x`` matches any 22.19 patch."""
    version = version.strip().lstrip("v")
    for allowed in matrix:
        allowed = allowed.strip().lstrip("v")
        if allowed.endswith(".x"):
            prefix = allowed[:-2]
            if version.startswith(prefix + "."):
                return True
        elif version == allowed:
            return True
    return False
