"""Control-artifact discovery: one resolution point.

A control artifact is a built file of one of the TypeScript control packages
(``dsh-eval-control``, ``deepagents-eval-control`` — the compiled stacks the
neutral ``aeval/control`` package feeds). Finding one used to be private
sibling-dir arithmetic in two places (``bootstrap.py`` for the facade dist,
``agents/dsh/agent.py`` for the session reader), each with its own search
order. This module is the single discipline:

1. an operator override — an environment variable naming either the artifact
   itself (``env_inner`` unset) or the package root (``env_inner`` set) — is
   always the first candidate;
2. otherwise the development layout: a sibling checkout of the aeval repo,
   found by walking up from an anchor (or at explicit anchors);
3. otherwise the installed layout: the package under ``node_modules``, passed
   as trailing ``extra`` candidates by the call site that knows its repo root.

Which package and which inner path a caller needs is flavor knowledge and
stays at the call site (the dsh flavor looks for ``dsh-eval-control``'s
session reader; the facade flavor for ``deepagents-eval-control``'s dist) —
what is shared, and what used to drift, is HOW a named package is searched
for. The candidate ORDER each historical call site produced is preserved
byte-for-byte: a resolved artifact must not move because discovery was
unified.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

__all__ = ["control_artifact_candidates"]


def control_artifact_candidates(
    *,
    env: str | None = None,
    env_inner: str | None = None,
    package: str,
    inner: str,
    start: Path | None = None,
    walk_from: Sequence[Path] = (),
    extra: Sequence[Path] = (),
) -> list[Path]:
    """Candidate artifact paths, most specific first (see module docstring).

    ``env`` names an environment variable; when set, its value contributes
    the first candidate — the artifact itself, or ``<value>/<env_inner>`` when
    the variable historically names the package root. ``package``/``inner``
    describe the artifact inside a discovered package (``dist`` for a whole
    dist directory, ``dist/session_reader.js`` for one file). The sibling
    walk starts at ``start`` (default: this module) and climbs through every
    parent, or probes exactly ``walk_from`` when given. ``extra`` candidates
    (the installed ``node_modules`` layout) are appended, deduplicated.
    """
    candidates: list[Path] = []
    if env is not None:
        override = os.environ.get(env, "").strip()
        if override:
            root = Path(override)
            candidates.append(root / env_inner if env_inner else root)
    if walk_from:
        anchors = [Path(anchor) for anchor in walk_from]
    else:
        anchor = Path(start) if start is not None else Path(__file__).resolve()
        anchors = [anchor, *anchor.parents]
    for parent in anchors:
        candidate = parent / package / inner
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in extra:
        candidate = Path(candidate)
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates
