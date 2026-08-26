"""milos exceptions."""

from __future__ import annotations


class MilosError(Exception):
    """Base class for all milos errors."""


class OptionsError(MilosError):
    """An AgentOptions value is invalid or unsupported in the sandbox."""


class PolicyError(MilosError):
    """A policy document is invalid, missing, or refuses the requested run."""


class SessionExists(MilosError):
    """A session document with that id is already there — the loser's half of a
    create race, which a caller holding a pre-assigned id may want to tolerate."""


class SessionTerminated(MilosError):
    """The remote session is terminated and cannot accept further input."""
