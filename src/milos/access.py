"""Who may do what.

Pure predicates over a session, and `Access` for what needs the directory:
membership of an agent's groups, and membership of the admin group that may
publish and enable definitions. The API calls these; the service re-checks
invariant 4 (an approver is never the operator) on its own.
"""

from .auth import Directory
from .models import Session


def can_view(session: Session, email: str) -> bool:
    """Operator, viewers and approvers see the session and its journal."""
    return email == session.operator or email in session.viewers or email in session.approvers


def can_operate(session: Session, email: str) -> bool:
    """Only the operator talks to the session: messages, interrupts, termination."""
    return email == session.operator


def can_decide(session: Session, email: str) -> bool:
    """Anyone but the operator, or only the named approvers when the session lists some."""
    if email == session.operator:
        return False
    return not session.approvers or email in session.approvers


class Access:
    """Group membership, or everything allowed when there is no directory (local development)."""

    def __init__(self, directory: Directory | None, *, admin_group: str | None = None) -> None:
        self._directory = directory
        self._admin_group = admin_group

    async def member(self, email: str, groups: list[str]) -> bool:
        if self._directory is None:
            return True
        for group in groups:
            if await self._directory.is_member(email, group):
                return True
        return False

    async def admin(self, email: str) -> bool:
        if self._directory is None:
            return True
        return bool(self._admin_group) and await self._directory.is_member(email, self._admin_group or "")
