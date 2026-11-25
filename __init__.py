"""hermes-vikunja — Vikunja as a Hermes gateway platform.

Loaded as a directory plugin (``~/.hermes/plugins/hermes-vikunja``): the gateway
imports this package and calls :func:`register` with its plugin context.
"""

from .adapter import register

__all__ = ["register"]
