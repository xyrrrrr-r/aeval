"""deepAgent adapter package (deepagents-code over ACP stdio)."""

from aeval.agents.deepagent.agent import (
    DcodeAgent,
    DcodeRunError,
    DcodeTrialPaths,
    default_deepagent_registry_entry,
)

__all__ = [
    "DcodeAgent",
    "DcodeRunError",
    "DcodeTrialPaths",
    "default_deepagent_registry_entry",
]
