"""Naming rules for the identifiers milos builds out of user input.

Agent names, policy versions, and incident ids all become Firestore document
ids or GCS prefixes; every user-supplied string that does so goes through one
of these functions, so options.py (client-side validation) and the store cannot
drift apart on what a valid name is.
"""

from __future__ import annotations

import re

from .errors import OptionsError

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def validate_name(kind: str, value: str) -> str:
    """Check a short stored-object name. `kind` names it in the error."""
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise OptionsError(
            f"{kind} must be a short name matching [a-z0-9][a-z0-9_-]* (max 64 chars), not a path"
        )
    return value
