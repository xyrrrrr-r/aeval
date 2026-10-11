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


OFFICIAL_DSH_TAG = "dsh-v0.2.0-rc.2"
OFFICIAL_DSH_COMMIT = "639ed015397290b3745d163aafe02ffee4aa3f84"

# Exact npm compatibility slice for DSH 0.2.0-rc.2. Beyond the
# packages the CLI itself ships, this pins every package the control plugin
# imports DIRECTLY inside the DSH process (defense 1 of the control-stack
# split): the plugin resolves these from DSH's own nested node_modules, so a
# DSH release that changes any of them silently changes what the plugin runs
# against — the lock must name them, never trust them transitively.
#
# The 0.2.0 metapackage stopped depending on @deepseek-ai/dsh-sdk-client and
# switched its sibling pins from caret ranges to exact versions, so this slice
# is now the resolved tree rather than a range-resolved guess. The one package
# the metapackage no longer ships at all is sdk-client: it is absent from a
# real 0.2.0-rc.2 install and nothing in the control plugin imports it, so it
# is dropped rather than pinned to something that is not there.
DSH_NPM_SLICE: tuple[tuple[str, str, str | None], ...] = (
    ("@deepseek-ai/dsh", "0.2.0-rc.2",
     "sha512-EAJ3gPNcVt/uv8X19PMm9NkVhWgT7xXNMk0UKCVm+IQ5rpSQOcsMUa0HWlnYYVybKMsccjcRB21vVVsaXQ6IdA=="),
    ("@deepseek-ai/dsh-sdk-protocol", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-acp", "0.2.0-rc.2", None),
    ("@agentclientprotocol/sdk", "1.4.0", None),
    ("@deepseek-ai/cordis", "4.0.4", None),
    # Direct imports of the control plugin (dsh-eval-control/src), all pinned
    # to the same release slice the plugin was compiled against:
    ("@deepseek-ai/dsh-agent", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-llm", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-scope", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-session", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-session-persistence", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-session-persistence-jsonl", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-system-prompt", "0.2.0-rc.2", None),
    ("@deepseek-ai/dsh-tools", "0.2.0-rc.2", None),
    ("@deepseek-ai/schemastery", "3.18.4", None),
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
        cordis_version="4.0.4",
        acp_sdk_version="1.4.0",
        experimental=True,
    )
