"""Errors raised by the platform. The API maps each to one HTTP status."""


class MilosError(Exception):
    status = 500


class NotFound(MilosError):
    status = 404


class AlreadyExists(MilosError):
    """A create-only document already exists (permission, approval, session)."""

    status = 409


class Conflict(MilosError):
    """Same idempotency key, different content."""

    status = 409


class Forbidden(MilosError):
    status = 403


class Unauthorized(MilosError):
    status = 401


class Invalid(MilosError):
    status = 422


class Stopped(MilosError):
    """The agent is disabled or the session is terminated; no further permissions."""

    status = 409
