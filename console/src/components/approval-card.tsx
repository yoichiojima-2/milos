"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { epoch } from "@/lib/types";
import type { PendingRequest, Verdict } from "@/lib/types";

// A parked tool call with a live countdown. `now` is the API's clock (see
// lib/api.ts): past `approval_expires_at` inspection has already recorded a
// timed-out deny, so the card says "expired" rather than offering a choice.
export function ApprovalCard({
  item,
  now,
  canDecide,
  linkSession = false,
  onDecide,
}: {
  item: PendingRequest;
  now: number;
  /** the signed-in user may decide (never the operator; only named approvers when set) */
  canDecide: boolean;
  /** set on the inbox page, where each card links to its session */
  linkSession?: boolean;
  onDecide: (toolUseId: string, verdict: Verdict) => void;
}) {
  const { session, call, request } = item;
  const deadline = epoch(session.approval_expires_at);
  const requested = epoch(request?.created_at) ?? deadline;
  const total = deadline && requested ? Math.max(1, deadline - requested) : 1;
  const left = deadline ? Math.max(0, deadline - now) : 0;
  const expired = deadline !== null && left <= 0;
  const urgent = !expired && left < 60;
  const minutes = Math.floor(left / 60);
  const seconds = Math.floor(left % 60)
    .toString()
    .padStart(2, "0");
  const args = request?.payload.args ?? null;

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-2xl border bg-card px-4 pt-3.5 pb-4",
        urgent ? "border-destructive/60" : "border-warn/50",
      )}
    >
      {deadline && (
        <div
          className={cn(
            "absolute top-0 left-0 h-[3px] transition-[width] duration-500 ease-linear",
            urgent ? "bg-destructive" : "bg-warn-dot",
          )}
          style={{ width: `${(left / total) * 100}%` }}
        />
      )}
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <b
          className={cn(
            "min-w-0 max-w-full truncate font-mono text-[13px] font-semibold",
            urgent ? "text-destructive" : "text-warn",
          )}
          title={call.tool_name}
        >
          {call.tool_name}
        </b>
        <span className="text-[10px] font-semibold tracking-[0.14em] text-muted-foreground uppercase">approval</span>
        {linkSession && (
          <Link
            href={`/session?sid=${session.session_id}`}
            className="font-mono text-[11px] text-muted-foreground underline-offset-2 hover:underline"
          >
            {shortId(session.session_id)} · {session.agent_id} · by {session.operator}
          </Link>
        )}
        <span
          className={cn("ml-auto font-mono text-xs tabular-nums", urgent ? "text-destructive" : "text-muted-foreground")}
        >
          {expired ? "expired" : deadline ? `${minutes}:${seconds} left` : ""}
        </span>
      </div>
      <pre className="my-2.5 max-h-40 overflow-y-auto rounded-xl bg-surface px-3 py-2.5 font-mono text-xs whitespace-pre-wrap text-muted-foreground [overflow-wrap:break-word]">
        {args === null ? "arguments not loaded yet" : JSON.stringify(args, null, 2)}
      </pre>
      {canDecide ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ok" disabled={expired} onClick={() => onDecide(call.tool_use_id, "allow")}>
            Allow
          </Button>
          <Button variant="destructive" disabled={expired} onClick={() => onDecide(call.tool_use_id, "deny")}>
            Deny
          </Button>
          <span className="font-mono text-[11px] text-faint" title={call.args_sha256}>
            sha256 {call.args_sha256.slice(0, 12)}…
          </span>
        </div>
      ) : (
        <p className="text-[12px] text-muted-foreground">
          Waiting for {session.approvers.length ? session.approvers.join(", ") : "someone in the agent's groups"} to
          decide. The operator cannot decide on their own session.
        </p>
      )}
    </div>
  );
}
