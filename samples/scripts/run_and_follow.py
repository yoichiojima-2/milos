"""Start a session and print its events as they happen.

    uv run python samples/scripts/run_and_follow.py analyst "Summarise last week's numbers."

Exits 0 when the session ends its turn, 2 when it is parked waiting for an
approval (run approve_pending.py as the approver), 1 on any other stop.
"""

from __future__ import annotations

import asyncio
import sys

from _identity import APPROVER, OPERATOR, client_as

from milos.models import Event, EventType, StopReason


def render(event: Event) -> str:
    p = event.payload
    match event.type:
        case EventType.AGENT_MESSAGE:
            return f"agent: {p.get('text', '')}"
        case EventType.AGENT_TOOL_USE:
            return f"tool {p.get('tool_name')}: {p.get('decision')} ({p.get('reason')})"
        case EventType.TOOL_RESULT:
            return f"  → {p.get('outcome')}: {str(p.get('summary', ''))[:120]}"
        case EventType.SESSION_STATUS:
            return f"status: {p.get('status')} {p.get('stop_reason') or ''}".rstrip()
        case EventType.SESSION_USAGE:
            return f"usage: turns={p.get('num_turns')} cost=${p.get('total_cost_usd', 0):.4f}"
        case _:
            return f"{event.type}: {p}"


async def main(agent_id: str, message: str) -> int:
    async with client_as(OPERATOR) as client:
        session = await client.create_session(agent_id, message, approvers=[APPROVER])
        print(f"session {session.session_id} ({agent_id} v{session.agent_version})")
        async for event in client.follow(session.session_id):
            print(f"{event.seq:>3}  {render(event)}")
        final = await client.session(session.session_id)
    print(f"stopped: {final.stop_reason}")
    if final.stop_reason == StopReason.REQUIRES_ACTION:
        print(f"waiting for approval of {', '.join(final.pending_tool_use_ids)}")
        return 2
    return 0 if final.stop_reason == StopReason.END_TURN else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
