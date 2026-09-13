"""Unattended weekly report: start the analyst, wait, print its final message.

    uv run python samples/scripts/weekly_report.py [ISO week, default: last week]

Idempotent per week: the client_request_id makes a retried run return the same
session instead of starting another. Approvals are not needed because the
prompt only uses tools the definition allows without a person (the data tools
and Write). A parked session is reported, not resolved, so a human decides.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

from _identity import APPROVER, OPERATOR, client_as

from milos.models import EventType, StopReason

PROMPT = (
    "Read weekly/{week}.csv and the week before it from the shared data project with "
    "list_files and read_file. Write report.md with, per region: orders, revenue, refunds "
    "and the week-over-week change in revenue as a percentage, then two sentences on what "
    "moved most and why it might have. Do not use Bash. Finish by replying with the report."
)


async def main(week: str) -> int:
    async with client_as(OPERATOR) as client:
        session = await client.create_session(
            "analyst", PROMPT.format(week=week), approvers=[APPROVER], client_request_id=f"weekly-report-{week}"
        )
        print(f"session {session.session_id} for {week}", file=sys.stderr)
        last_message = ""
        async for event in client.follow(session.session_id):
            if event.type == EventType.AGENT_MESSAGE:
                last_message = str(event.payload.get("text", ""))
        final = await client.session(session.session_id)
    if final.stop_reason == StopReason.REQUIRES_ACTION:
        print(f"parked: {final.session_id} waits for approval of {final.pending_tool_use_ids}", file=sys.stderr)
        return 2
    print(last_message)
    return 0 if final.stop_reason == StopReason.END_TURN else 1


if __name__ == "__main__":
    default = (dt.date.today() - dt.timedelta(weeks=1)).isocalendar()
    week = sys.argv[1] if len(sys.argv) > 1 else f"{default.year}-W{default.week:02d}"
    sys.exit(asyncio.run(main(week)))
