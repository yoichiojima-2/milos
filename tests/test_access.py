"""Authorization predicates: who may view, operate and decide."""

from __future__ import annotations

from milos.access import Access, can_decide, can_operate, can_view

from .fakes import FakeDirectory


def test_predicates(session):
    alice, lead, stranger = "alice@example.com", "lead@example.com", "x@example.com"
    open_session = session.model_copy(update={"viewers": ["v@example.com"]})
    assert can_view(open_session, alice) and can_view(open_session, "v@example.com") and not can_view(open_session, lead)
    assert can_operate(open_session, alice) and not can_operate(open_session, "v@example.com")
    # no approver list: anyone but the operator; a list: only those named
    assert can_decide(open_session, stranger) and not can_decide(open_session, alice)
    named = session.model_copy(update={"approvers": [lead]})
    assert can_view(named, lead) and can_decide(named, lead) and not can_decide(named, stranger)


async def test_access_checks_groups_and_is_permissive_without_a_directory():
    directory = FakeDirectory({"analysts@example.com": ["*@example.com"], "admins@example.com": ["admin@example.com"]})
    access = Access(directory, admin_group="admins@example.com")
    assert await access.member("a@example.com", ["analysts@example.com"])
    assert not await access.member("a@other.org", ["analysts@example.com"])
    assert await access.admin("admin@example.com") and not await access.admin("a@example.com")
    assert not await Access(directory).admin("admin@example.com")  # no admin group configured: nobody is admin
    local = Access(None)
    assert await local.member("anyone@x", ["g"]) and await local.admin("anyone@x")
