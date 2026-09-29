#!/usr/bin/env python3
"""Re-verify the standing invariants against the runs actually left on disk.

Two claims are made constantly and are worth re-checking after every change to the
core, because both fail *quietly*:

1. **Previously sealed evidence still recomputes.** A bundle sealed before a change
   must still verify after it — otherwise the change rewrote history. Negative
   fixtures (tamper tests) are expected to fail and are attributed by test name,
   never waved away.
2. **The runtime lock digest of existing artifacts is unchanged.** Adding a section
   to ``RuntimeLock`` must not change the digest of locks that were recorded before
   the section existed, or every sealed digest stops matching.

Usage:
    .venv/bin/python tools/audit_sealed_evidence.py [--root ../tools/test-tmp]

Exit code is non-zero when a *non-negative* artifact fails: this is meant to be
runnable in CI and to be believed.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aeval.bundle.attestation import recompute_bundle  # noqa: E402
from aeval.contracts import RuntimeLock  # noqa: E402

#: A test whose name says it must fail is a fixture, not a regression. This is an
#: explicit list on purpose: an unattributed failure must stay visible.
#: pytest truncates the temp-dir name at 30 chars, so markers are matched as
#: prefixes/substrings of a possibly-truncated name.
NEGATIVE_TEST_MARKERS = (
    "reject", "detect", "tamper", "double_seal", "without_attest",
)


def test_name_of(path: Path) -> str:
    """``…/pytest-300/test_foo0/run/x.json`` -> ``test_foo``."""
    for part in path.parts:
        if re.fullmatch(r"[a-zA-Z_]\w{5,}0", part) and not part.startswith("pytest"):
            return part[:-1]
    return "<unknown>"


def is_negative(test_name: str) -> bool:
    return any(marker in test_name for marker in NEGATIVE_TEST_MARKERS)


def _has_bundle_content(run_dir: Path) -> bool:
    """Whether a sealed manifest has any bundle to recompute at all.

    A unit fixture may seal a manifest on its own (asserting the seal digest).
    That is out of scope here, and is counted separately rather than passed
    silently or reported as a failure.
    """
    return any(
        path.name != "run_manifest.json"
        for path in run_dir.rglob("*")
        if path.is_file()
    )


def audit_bundles(root: Path) -> tuple[int, int, list[str], int]:
    verified, skipped, manifest_only = 0, 0, 0
    problems: list[str] = []
    for path in sorted(root.rglob("run_manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (data.get("sealed_at") or data.get("sealed")):
            skipped += 1
            continue
        name = test_name_of(path)
        if not is_negative(name) and not _has_bundle_content(path.parent):
            manifest_only += 1
            continue
        try:
            ok = getattr(recompute_bundle(path.parent), "ok", True)
        except Exception as exc:  # noqa: BLE001 - a rejection is the expected outcome here
            ok = False
            detail = f"{type(exc).__name__}: {str(exc)[:90]}"
        else:
            detail = "report not ok"
        if ok:
            verified += 1
        elif not is_negative(name):
            problems.append(f"{name}: {detail}")
    return verified, skipped, problems, manifest_only


def audit_locks(root: Path) -> tuple[int, int, list[str]]:
    recorded: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            value = node.get("runtime_lock_digest")
            if isinstance(value, str) and value:
                recorded.add(value)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in root.rglob("*.json"):
        if path.name == "runtime_lock.json":
            continue
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    matched, problems = 0, []
    for path in sorted(root.rglob("runtime_lock.json")):
        try:
            digest = RuntimeLock.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            ).digest()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{test_name_of(path)}: unreadable lock ({type(exc).__name__})")
            continue
        if digest in recorded:
            matched += 1
        elif not is_negative(test_name_of(path)):
            problems.append(
                f"{test_name_of(path)}: digest {digest[:12]} matches no recorded value"
            )
    return matched, len(recorded), problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("../tools/test-tmp"))
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        print(f"no artifacts under {root} — run the suite first")
        return 0

    verified, skipped, bundle_problems, manifest_only = audit_bundles(root)
    print(
        f"sealed bundles recomputed: {verified} "
        f"(unsealed: {skipped}, manifest-only seals with no bundle: {manifest_only})"
    )
    matched, distinct, lock_problems = audit_locks(root)
    print(f"lock digests matched to a recorded value: {matched} ({distinct} distinct values)")

    problems = bundle_problems + lock_problems
    for line in problems:
        print(f"  UNATTRIBUTED FAILURE: {line}")
    if problems:
        print(f"{len(problems)} artifact(s) failed outside a known negative fixture")
        return 1
    print("every artifact failure is attributable to a negative fixture; invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
