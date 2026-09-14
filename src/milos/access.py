"""Who may do what. Pure predicates over a session; the API calls them, the service re-checks invariant 4."""

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
