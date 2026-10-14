#!/usr/bin/env python3
"""Pin the two levels of image digests into the terminal-beach suite.

The e2b backend uses ``[environment] docker_image`` verbatim, so the digests are
the ONLY thing that decides what a trial runs — editing them by hand is how a
silently-ignored Dockerfile survives review. This helper is the single writer:

    pin-digests.py base <ref@sha256:...>              FROM in every task Dockerfile
    pin-digests.py task <task> <ref@sha256:...>       docker_image in that task.toml
    pin-digests.py show                               the current pin table

Both writers are idempotent and refuse a digest-less reference: a tag can move
under a running suite, a digest cannot.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parents[1]
TASKS = ("hello-world", "openssl-selfsigned-cert", "sqlite-db-truncate")
NAME = "terminal-beach"


def _require_digest(reference: str) -> str:
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", reference):
        raise SystemExit(f"FATAL: reference must be digest-pinned, got {reference!r}")
    return reference


def _task_dockerfile(task: str) -> Path:
    return SUITE / "tasks" / task / "environment" / "Dockerfile"


def _task_toml(task: str) -> Path:
    return SUITE / "tasks" / task / "task.toml"


def set_base(reference: str) -> None:
    reference = _require_digest(reference)
    for task in TASKS:
        path = _task_dockerfile(task)
        text = path.read_text("utf-8")
        pinned, count = re.subn(
            r"^FROM\s+\S+@sha256:[0-9a-f]{64}\s*$",
            f"FROM {reference}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise SystemExit(f"FATAL: no pinned FROM line in {path}")
        path.write_text(pinned, "utf-8")
        print(f"base  -> {path.relative_to(SUITE)}")


def set_task(task: str, reference: str) -> None:
    if task not in TASKS:
        raise SystemExit(f"FATAL: unknown task {task!r}")
    reference = _require_digest(reference)
    path = _task_toml(task)
    text = path.read_text("utf-8")
    pinned, count = re.subn(
        r'^docker_image\s*=\s*"[^"]+"\s*$',
        f'docker_image = "{reference}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"FATAL: no docker_image line in {path}")
    path.write_text(pinned, "utf-8")
    print(f"task  -> {path.relative_to(SUITE)}")


def show() -> None:
    for task in TASKS:
        from_line = next(
            line for line in _task_dockerfile(task).read_text("utf-8").splitlines()
            if line.startswith("FROM ")
        )
        from_digest = from_line.split("@sha256:")[1][:12]
        image_line = next(
            line for line in _task_toml(task).read_text("utf-8").splitlines()
            if line.startswith("docker_image")
        )
        image_digest = image_line.split("@sha256:")[1][:12]
        print(f"{task:24s} from={from_digest}…  docker_image={image_digest}…")


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["show"]:
        show()
    elif len(argv) == 2 and argv[0] == "base":
        set_base(argv[1])
    elif len(argv) == 3 and argv[0] == "task":
        set_task(argv[1], argv[2])
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
