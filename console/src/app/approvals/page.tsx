"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApprovalCard } from "@/components/approval-card";
import { useAction, useInbox, useMe, useNow } from "@/lib/hooks";
import { post } from "@/lib/api";
import { canDecide } from "@/lib/types";
import type { Verdict } from "@/lib/types";

export default function ApprovalsPage() {
  const { inbox, dismiss } = useInbox();
  const me = useMe();
  const now = useNow();
  const [flash, run] = useAction();

  const decide = (sessionId: string, toolUseId: string, verdict: Verdict) =>
    run(async () => {
      await post(`/v1/sessions/${sessionId}/approvals`, { tool_use_id: toolUseId, verdict });
      dismiss(toolUseId);
      return verdict === "allow" ? "allowed" : "denied";
    });

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <h1 className="font-serif text-2xl tracking-tight">Approvals</h1>
        <p className="pt-1 text-[12px] text-muted-foreground">
          Tool calls parked in sessions that name you as an approver — the same list as{" "}
          <code className="font-mono text-xs">milos pending</code>. A session with no named approvers is decided
          from its own page by anyone in the agent&apos;s groups.
        </p>
      </div>
      {flash && <p className="text-[11px] text-muted-foreground">{flash}</p>}
      {inbox === null ? (
        <Skeleton className="h-32 w-full max-w-3xl" />
      ) : inbox.length === 0 ? (
        <Card className="max-w-3xl">
          <CardContent className="py-16 text-center text-[13px] text-muted-foreground">
            Nothing is waiting on you.
          </CardContent>
        </Card>
      ) : (
        <div className="max-w-3xl space-y-3">
          {inbox.map((item) => (
            <ApprovalCard
              key={`${item.session.session_id}/${item.call.tool_use_id}`}
              item={item}
              now={now}
              canDecide={!!me && canDecide(item.session, me.email)}
              linkSession
              onDecide={(toolUseId, verdict) => decide(item.session.session_id, toolUseId, verdict)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
