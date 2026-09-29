#!/usr/bin/env python3
"""Prepare the offline wheelhouse the deepagent task images install from.

Why this exists: the lab host has no PyPI egress (npm and the internal docker
registry are reachable, pypi.org is not), and every suite runs with
``network_mode = "no-network"``, so ``uvx deepagents-code == ...`` can never
fetch anything at agent setup. The images therefore bake the pinned CLI in at
build time — and the build needs the packages to already be on the host.

The closure is resolved for the *image* platform, not the operator's: pass
``--platform`` tags for the sandbox (the lab builds aarch64). Resolution is done
by uv because it is the same resolver ``uvx`` would use; the download is done
per package because pip refuses to mix platform constraints with source
distributions, and one dependency of this closure ships sdist-only
(``forbiddenfruit``), which is exactly what makes the naive
``pip download --only-binary=:all:`` look like an unsatisfiable conflict.

Usage (from the repo root, on a machine WITH PyPI access):

    python3 tools/fetch_deepagent_wheelhouse.py \
        --dest suites/deepagent-budget/tasks/hello/environment/wheelhouse

Then copy the same directory beside the other deepagent suite's Dockerfile, or
pass ``--also`` for each suite the images belong to.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_PLATFORMS = (
    "manylinux_2_17_aarch64",
    "manylinux_2_28_aarch64",
    "manylinux_2_34_aarch64",
    "manylinux2014_aarch64",
    "linux_aarch64",
    "any",
)
# pip needs these inside the isolated build environment it creates for the
# sdist-only packages (Python 3.12 venvs no longer seed setuptools).
BUILD_REQUIREMENTS = ("setuptools", "wheel", "setuptools_scm", "hatchling", "flit_core")


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, **kwargs)  # noqa: S603


def _resolve(pin: str, python_version: str, platform: str, uv: str) -> list[str]:
    """The full pinned closure, resolved for the target platform by uv."""
    completed = _run(
        [uv, "pip", "compile", "-", "--python-platform", platform,
         "--python-version", python_version],
        input=f"{pin}\n",
    )
    if completed.returncode != 0:
        sys.exit(f"resolution failed:\n{completed.stderr.strip()}")
    return [
        line.strip() for line in completed.stdout.splitlines()
        if "==" in line and not line.startswith("#") and not line.startswith(" ")
    ]


def _download_wheel(package: str, dest: Path, python_version: str, platforms: tuple[str, ...]) -> bool:
    command = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
               "--dest", str(dest), "--python-version", python_version,
               "--implementation", "cp", "--abi", "cp312", "--abi", "none"]
    for platform in platforms:
        command += ["--platform", platform]
    command.append(package)
    return _run(command).returncode == 0


def _download_sdist(package: str, dest: Path) -> bool:
    return _run(
        [sys.executable, "-m", "pip", "download", "--no-deps", "--no-binary", ":all:",
         "--dest", str(dest), package]
    ).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", default="deepagents-code==0.1.78")
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--platform", default="aarch64-unknown-linux-gnu",
                        help="the uv --python-platform value for the sandbox")
    parser.add_argument("--platform-tag", action="append", default=None,
                        help="pip --platform tag (repeatable; defaults to the aarch64 set)")
    parser.add_argument("--dest", required=True)
    parser.add_argument("--also", action="append", default=[],
                        help="extra directories to copy the finished wheelhouse into")
    parser.add_argument("--uv", default=shutil.which("uv") or "uv")
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    platforms = tuple(args.platform_tag) if args.platform_tag else DEFAULT_PLATFORMS

    pins = _resolve(args.pin, args.python_version, args.platform, args.uv)
    (dest / "pins.txt").write_text("\n".join([args.pin, *pins]) + "\n", encoding="utf-8")
    print(f"resolved {len(pins)} packages for {args.platform}")

    sdists: list[str] = []
    failed: list[str] = []
    for pin in pins:
        name = re.split(r"[=<>!\[]", pin, maxsplit=1)[0]
        if _download_wheel(pin, dest, args.python_version, platforms):
            continue
        if _download_sdist(pin, dest):
            sdists.append(pin)
            continue
        failed.append(pin)
    for requirement in BUILD_REQUIREMENTS:
        _download_wheel(requirement, dest, args.python_version, platforms)

    if failed:
        print(f"could not fetch: {failed}", file=sys.stderr)
        return 1
    wheels = len(list(dest.glob("*.whl")))
    size = sum(path.stat().st_size for path in dest.iterdir() if path.is_file()) / (1024 * 1024)
    print(f"wheelhouse: {wheels} wheels, {len(sdists)} sdists ({size:.1f} MiB) in {dest}")
    if sdists:
        print("built from source at image build time: " + ", ".join(sdists))
    for extra in args.also:
        target = Path(extra).resolve()
        target.mkdir(parents=True, exist_ok=True)
        for path in dest.iterdir():
            if path.is_file():
                shutil.copy2(path, target / path.name)
        print(f"copied into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
