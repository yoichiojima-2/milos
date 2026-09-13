"""Act as the approver: decide every parked tool call by a small policy.

    uv run python samples/scripts/approve_pending.py            # decide once and exit
    uv run python samples/scripts/approve_pending.py --watch    # keep deciding every 10 s

The policy is deliberately simple and visible: Bash is allowed only when the
command is read-only (ls, cat, head, wc, awk over the working directory),
web_fetch only for an allow-listed host, everything else is denied. The
approver must not be the session's operator; the API rejects that.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from typing import Any

from _identity import APPROVER, client_as

from milos.models import EventType, SessionStatus, ToolDecision

READ_ONLY = {"ls", "cat", "head", "tail", "wc", "awk", "sort", "uniq", "pwd", "find", "grep"}
FETCH_HOSTS = {"www.bankofjapan.or.jp", "example.com"}


def decide(tool_name: str, args: dict[str, Any]) -> tuple[ToolDecision, str]:
    if tool_name == "Bash":
        words = [w for w in shlex.split(str(args.get("command", ""))) if w not in ("|", "&&", ";")]
        first_words = {words[0]} | {w for i, w in enumerate(words) if i and words[i - 1] in ("|", "&&", ";")}
        return (
            ("allow", "read-only command")
            if first_words <= READ_ONLY and "/" not in " ".join(words).replace("./", "")
            else ("deny", "not read-only")
        )
    if tool_name == "mcp__egress__web_fetch":
        host = str(args.get("url", "")).split("/")[2] if "://" in str(args.get("url", "")) else ""
        return ("allow", "allow-listed host") if host in FETCH_HOSTS else ("deny", f"host {host!r} not allow-listed")
    return ("deny", "no policy for this tool")


async def decide_once() -> int:
    decided = 0
    async with client_as(APPROVER) as client:
        for session in await client.sessions():
            if session.status != SessionStatus.IDLE or not session.pending_tool_use_ids:
                continue
            # The request's arguments are in the journaled agent.tool_use event.
            requests = {
                e.tool_use_id: e.payload for e in await client.events(session.session_id) if e.type == EventType.AGENT_TOOL_USE
            }
            for tool_use_id in session.pending_tool_use_ids:
                payload = requests.get(tool_use_id, {})
                decision, why = decide(str(payload.get("tool_name")), payload.get("args") or {})
                approval = await client.confirm(session.session_id, tool_use_id, decision)
                print(f"{session.session_id} {approval.tool_name} {tool_use_id}: {decision} ({why})")
                decided += 1
    return decided


async def main(watch: bool) -> None:
    while True:
        n = await decide_once()
        if not watch:
            print(f"decided {n}")
            return
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main("--watch" in sys.argv))
