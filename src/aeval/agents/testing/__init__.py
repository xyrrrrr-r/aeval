"""Reference adapters for conformance testing.

Shipped in the package rather than the test tree on purpose: an adapter is
addressed by ``import_path`` from a Harbor job, so a reference adapter must be
importable exactly the way a real one is.
"""
