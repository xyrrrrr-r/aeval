"""DSH adapter: official SessionPersistence bridge, ATIF mapping, claims.

Importing this package also registers the dsh control-stack flavor
(:mod:`aeval.agents.dsh.control_flavor`) — the adapter's deployment
specifics travel with the adapter, so the framework core never imports it.
"""
from aeval.agents.dsh import control_flavor as _control_flavor  # noqa: F401

__all__ = ["control_flavor"]
