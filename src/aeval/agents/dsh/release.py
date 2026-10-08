"""The DSH official release pin: this adapter's supply-chain facts.

Moved out of ``aeval.provenance``: the exact
official DSH preview slice — npm packages + integrity, Node versions,
Cordis/ACP versions, lockfile digest — is DSH's own fact, so it lives in
DSH's adapter package and reaches the runtime lock through the adapter's
``OFFICIAL_RELEASE_LOCK`` hook (``aeval.agents.contract``). The core
provenance module builds locks from whatever locks the selected adapters
declare; it names no agent.

``DshReleaseLock`` (the legacy lock shape) stays in ``aeval.contracts``:
it is part of the sealed runtime-lock FORMAT — old locks must keep loading
and digesting identically — and the projection to/from the generic
``AgentReleaseLock`` lives with the format, not with the agent.

DSH is experimental and must never auto-upgrade.
"""

from __future__ import annotations

from aeval.contracts import DshReleaseLock, NpmPackageLock

__all__ = [
    "OFFICIAL_DSH_TAG",
    "OFFICIAL_DSH_COMMIT",
    "DSH_NPM_SLICE",
    "DSH_CONTROL_DIRECT_IMPORTS",
    "DSH_NODE_VERSIONS",
    "build_official_dsh_lock",
]


OFFICIAL_DSH_TAG = "dsh-v0.1.7-alpha.1"
OFFICIAL_DSH_COMMIT = "c36a83ff6bb95e3f82cf79f9be7c724270a8aa61"

# Exact npm compatibility slice for DSH 0.1.7-alpha.1. Beyond the
# packages the CLI itself ships, this pins every package the control plugin
# imports DIRECTLY inside the DSH process (defense 1 of the control-stack
# split): the plugin resolves these from DSH's own nested node_modules, so a
# DSH release that changes any of them silently changes what the plugin runs
# against — the lock must name them, never trust them transitively.
DSH_NPM_SLICE: tuple[tuple[str, str, str | None], ...] = (
    ("@deepseek-ai/dsh", "0.1.7-alpha.1",
     "sha512-fim76775kLyal0lLNmpktZfOiOwU0P9qdluknL5Sm3F6ax9I5PcLD0W0WzqH9tMOOY8yHya5VShuEzSSh223sw=="),
    ("@deepseek-ai/dsh-sdk-client", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-sdk-protocol", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-acp", "0.1.7-alpha.1", None),
    ("@agentclientprotocol/sdk", "1.4.0", None),
    ("@deepseek-ai/cordis", "4.0.3", None),
    # Direct imports of the control plugin (dsh-eval-control/src), all pinned
    # to the same release slice the plugin was compiled against:
    ("@deepseek-ai/dsh-agent", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-llm", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-scope", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-session", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-session-persistence", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-session-persistence-jsonl", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-system-prompt", "0.1.7-alpha.1", None),
    ("@deepseek-ai/dsh-tools", "0.1.7-alpha.1", None),
    ("@deepseek-ai/schemastery", "3.18.3", None),
)

# The control stack's complete direct import surface: the union of the DSH
# package's own sources (dsh-eval-control/src, non-generated files, after the
# slim-down at 5d33d43) and the neutral broker cluster (aeval/control/src,
# whose dist the DSH package composes into its deployment unit). Kept beside
# the slice so the "import surface ⊆ slice" invariant is checkable without
# the sibling checkout; the sibling test re-measures reality against this
# list so a new import cannot appear unrecorded.
DSH_CONTROL_DIRECT_IMPORTS: tuple[str, ...] = (
    "@deepseek-ai/cordis",
    "@deepseek-ai/dsh-agent",
    "@deepseek-ai/dsh-llm",
    "@deepseek-ai/dsh-scope",
    "@deepseek-ai/dsh-session",
    "@deepseek-ai/dsh-session-persistence",
    "@deepseek-ai/dsh-session-persistence-jsonl",
    "@deepseek-ai/dsh-system-prompt",
    "@deepseek-ai/dsh-tools",
    "@deepseek-ai/schemastery",
)

DSH_NODE_VERSIONS = ("22.19.x", "24.20.0")


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
