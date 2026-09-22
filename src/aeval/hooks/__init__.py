"""Harbor job plugin and trial hooks for aeval.

The production entrypoint is the full plugin import path:
``harbor jobs run --plugin aeval.hooks:AevalPlugin``.
"""

from aeval.hooks.plugin import AevalPlugin

__all__ = ["AevalPlugin"]
