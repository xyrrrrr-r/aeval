"""deepAgent adapter package (deepagents-code over ACP stdio, P2-5a)."""

from aeval.agents.deepagent.agent import (
    DeepgentAgent,
    DeepgentRunError,
    DeepgentTrialPaths,
    default_deepagent_registry_entry,
)

__all__ = [
    "DeepgentAgent",
    "DeepgentRunError",
    "DeepgentTrialPaths",
    "default_deepagent_registry_entry",
]
