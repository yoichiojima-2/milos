"use client";

import Link from "next/link";
import { Bot, CircleStop, OctagonX } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { StateBadge } from "@/components/state-badge";
import { Transcript } from "@/components/transcript";
import { Composer } from "@/components/composer";
import { ApprovalCard } from "@/components/approval-card";
import { useAction, useMe, useNow, useSessionFeed } from "@/lib/hooks";
import { post } from "@/lib/api";
import { shortId } from "@/lib/format";
import { canDecide, canOperate, sessionState } from "@/lib/types";
import type { Event, Verdict } from "@/lib/types";

export function SessionDetail({ sid }: { sid: string }) {
  const { session, events, pending, missing, refresh } = useSessionFeed(sid);
  const me = useMe();
  const now = useNow();
  const [flash, run] = useAction();

  if (missing)
    return (
      <p className="pt-20 text-center text-[13px] text-muted-foreground">
        No such session, or you are not a participant of it.
      </p>
    );

  const state = session ? sessionState(session) : null;
  const dead = state === "terminated";
  const operator = !!(session && me && canOperate(session, me.email));
  const decider = !!(session && me && canDecide(session, me.email));
  // The agent owes a response while it runs and the newest event is not yet
  // its answer or a stop — that's when the typing dots show.
  const working = state === "running" && !answered(events);

  const decide = (toolUseId: string, verdict: Verdict) =>
    run(async () => {
      await post<Event>(`/v1/sessions/${sid}/approvals`, { tool_use_id: toolUseId, verdict });
      refresh();
      return verdict === "allow" ? "allowed" : "denied";
    });

  const send = async (text: string) => {
    let failed = false;
    await run(async () => {
      try {
        await post<Event>(`/v1/sessions/${sid}/messages`, { text });
        refresh();
      } catch (err) {
        failed = true;
        throw err;
      }
    });
    // run() turns the failure into a flash; rejecting here hands the text back to the composer
    if (failed) throw new Error("message not sent");
  };

  const interrupt = () =>
    run(async () => {
      await post<Event>(`/v1/sessions/${sid}/interrupt`, {});
      return "interrupt journaled";
    });

  const terminate = () => {
    if (!confirm(`Terminate ${sid}? Every later tool call is denied and the session cannot resume.`)) return;
    run(async () => {
      await post(`/v1/sessions/${sid}/terminate`);
      refresh();
      return "terminated";
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border bg-surface px-3 py-2 sm:gap-2.5 sm:px-5 sm:py-3">
        <span className="min-w-0 max-w-full truncate font-mono text-[13px]" title={sid}>
          {shortId(sid)}
        </span>
        {session && state && (
          <>
            <StateBadge state={state} />
            <Link
              href={`/agent?id=${encodeURIComponent(session.agent_id)}`}
              className="hover:opacity-80"
              title={`Runs ${session.agent_id} version ${session.agent_version}`}
            >
              <Badge className="font-mono">
                <Bot className="size-3" />
                {session.agent_id}
                <span className="text-muted-foreground">v{session.agent_version}</span>
              </Badge>
            </Link>
            <span className="font-mono text-[11px] text-muted-foreground">
              by {session.operator}
              {session.approvers.length ? ` · approvers ${session.approvers.join(", ")}` : ""}
              {session.lease ? ` · runner ${session.lease.runner_id}` : ""}
            </span>
          </>
        )}
        <span className="flex-1" />
        {operator && (
          <>
            <Button variant="outline" size="sm" onClick={interrupt} disabled={!session || dead} title="Interrupt">
              <CircleStop /> <span className="hidden sm:inline">Interrupt</span>
            </Button>
            <Button variant="destructive" size="sm" onClick={terminate} disabled={!session || dead} title="Terminate">
              <OctagonX /> <span className="hidden sm:inline">Terminate</span>
            </Button>
          </>
        )}
      </div>

      <Transcript events={events} placeholder={session ? "No events yet." : "loading…"} working={working} />

      {pending.length > 0 && (
        <div className="mx-auto w-full max-w-3xl space-y-2.5 px-5 pt-2.5">
          {pending.map((item) => (
            <ApprovalCard key={item.call.tool_use_id} item={item} now={now} canDecide={decider} onDecide={decide} />
          ))}
        </div>
      )}

      {flash && <p className="pt-1.5 text-center text-[11px] text-muted-foreground">{flash}</p>}

      {operator ? (
        <Composer
          disabled={!session || dead}
          onSend={send}
          placeholder={state === "idle" ? "Send a message — the runner restarts for it…" : "Send a message…"}
        />
      ) : (
        <p className="px-5 pt-2 pb-4 text-center text-[11px] text-faint">
          Only the operator sends messages to a session.
        </p>
      )}
    </div>
  );
}

/** True once the newest journal entry is the agent's answer or a stop, not a request still in flight. */
function answered(events: Event[]): boolean {
  const last = events[events.length - 1];
  return !last || last.type === "agent.message" || last.type === "session.usage" || last.type === "session.status";
}
