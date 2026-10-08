"""The dsh adapter's transcript-extra vocabulary.

``CanonicalTranscript.atif.extra`` is a keyed namespace: the framework owns
``aeval`` (``AEVAL_EXTRA_KEY`` in aeval.contracts) for its own claims, and
every adapter owns its own keys for the facts only it can read or write.
These two — the dsh session's structured facts and the events preserved
verbatim because the canonical model has no place for them — are the dsh
adapter's. They live here, in the adapter package, so a second agent extends
the vocabulary from its own package instead of editing the core contract
module.
"""

from __future__ import annotations

__all__ = [
    "DSH_EXTRA_KEY",
    "DSH_PRESERVED_EVENT_EXTRA_KEY",
]

DSH_EXTRA_KEY = "dsh"
DSH_PRESERVED_EVENT_EXTRA_KEY = "dsh_preserved_events"
