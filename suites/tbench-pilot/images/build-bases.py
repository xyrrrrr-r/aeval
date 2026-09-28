#!/usr/bin/env python3
"""Stage and build the adapted pilot base image; print its digest.

Runs on the cluster host. The image build itself needs NO network: the
Node.js tree, the uv binary, the wheelhouse, the verifier project and the
npm cache are all staged into the build context, and the base image comes
from the internal registry. The wheelhouse is fetched HERE, once, over the
host's (throttled) uplink — it is ~2 MB, which is why this step is
acceptable where a ~250 MB upstream base pull is not. The npm cache is
NOT fetched here: it is the cluster host's existing ~300 MB cache, which
is already complete for the pinned CLI (verified by an offline install),
because fetching it over the throttled registry would take hours.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

WORK = Path("/root/e2e/tbench-ctx")
UPSTREAM = "193.126.4.2:2900/e2b-orchestration/ubuntu:22.04-custom"
REF = "193.126.4.2:2900/t-bench/base-pilot:0.1.1"
NODE_TREE = Path("/root/e2e/tools/node-v24.20.0-linux-arm64")
UV_BIN = Path("/usr/local/bin/uv")
WHEELS = ("pytest==9.1.1", "exceptiongroup==1.3.1")
NPM_CACHE = Path("/root/.npm")
AEVAL_PYTHON = Path("/root/src/aeval/.venv/bin/python")
NPMRC = Path(__file__).resolve().parent / "npmrc"
RECIPE = Path(__file__).resolve().parent / "base-image.Dockerfile"
VERIFIER_SRC = Path("/root/e2e/tbench-ctx/verifier-src")
# tomli ships a pure-Python wheel AND compiled cp311 wheels; pip picks the
# compiled one natively, which cannot install on the image's Python 3.10.
# The cross-target flags below force the pure wheel.
CROSS_TARGET = ("tomli==2.4.1",)
CROSS_FLAGS = (
    "--no-deps", "--only-binary", ":all:", "--python-version", "3.10",
    "--implementation", "py", "--abi", "none", "--platform", "any",
)


def run(cmd: list[str], **kwargs) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def stage_wheelhouse() -> None:
    wheels = WORK / "wheels"
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(parents=True)
    run([sys.executable, "-m", "pip", "download", "-q", "-d", str(wheels), *WHEELS])
    run([sys.executable, "-m", "pip", "download", "-q", "-d", str(wheels),
         *CROSS_FLAGS, *CROSS_TARGET])
    # Belt and braces: no interpreter-specific wheel may survive staging.
    for wheel in wheels.glob("*cp3*"):
        print("removing non-universal wheel:", wheel.name, flush=True)
        wheel.unlink()
    names = sorted(p.name for p in wheels.iterdir())
    print("wheelhouse:", names, flush=True)
    if not names:
        raise SystemExit("wheelhouse is empty")


def locked_dsh_version() -> str:
    """The DSH version the adapter's official lock pins.

    Read from aeval itself (the same code path a trial uses), so the
    warmed npm cache cannot drift from what the sandbox installs.
    """
    out = subprocess.run(
        [str(AEVAL_PYTHON), "-c",
         "from aeval.agents.dsh.agent import DshAgent;"
         " print(DshAgent._locked_version())"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        raise SystemExit("could not read the pinned DSH version from aeval")
    print("pinned DSH version:", out, flush=True)
    return out


def stage_npm_cache() -> None:
    """Stage the cluster host's complete, registry-keyed npm cache.

    Complete is not an assumption: the Dockerfile's build-time self-check
    runs an `--offline` install of the pinned CLI from this cache, so an
    incomplete cache fails the build instead of a sealed trial.
    """
    if not NPM_CACHE.is_dir():
        raise SystemExit(f"no npm cache at {NPM_CACHE}; run the CLI install first")
    cache = WORK / "npm-cache"
    if cache.exists():
        shutil.rmtree(cache)
    shutil.copytree(NPM_CACHE, cache, symlinks=True)
    # The recipe and the npm config are copied from THIS directory, so the
    # build context can never carry a stale copy (it did: the context used
    # to keep whatever Dockerfile was last dropped into it by hand).
    shutil.copy2(NPMRC, WORK / "npmrc")
    shutil.copy2(RECIPE, WORK / "base-image.Dockerfile")
    print("npm cache staged:", sum(1 for _ in cache.rglob("*")), "entries", flush=True)


def stage_assets() -> None:
    node = WORK / "node"
    if node.exists():
        shutil.rmtree(node)
    shutil.copytree(NODE_TREE, node, symlinks=True)
    shutil.copy2(UV_BIN, WORK / "uv")
    (WORK / "uv").chmod(0o755)
    # The verifier project is STAGED FROM A SEPARATE SOURCE directory:
    # copying it from WORK/"verifier" (its own destination) would delete
    # the source first and fail — found on the first run of this script.
    verifier = WORK / "verifier"
    if verifier.exists():
        shutil.rmtree(verifier)
    shutil.copytree(VERIFIER_SRC, verifier)


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    stage_wheelhouse()
    stage_assets()
    stage_npm_cache()
    run(["docker", "build", "--platform", "linux/arm64",
         "--build-arg", f"UPSTREAM={UPSTREAM}",
         "--build-arg", f"DSH_VERSION={locked_dsh_version()}",
         "-f", str(WORK / "base-image.Dockerfile"), "-t", REF, str(WORK)])
    run(["docker", "push", REF])
    digest = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", REF],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    print("=== digest ===")
    print(digest)
    print("BASE_BUILD_DONE")


if __name__ == "__main__":
    main()
