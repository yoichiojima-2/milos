"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { StateBadge } from "@/components/state-badge";
import { relTime, shortId } from "@/lib/format";
import { epoch, sessionState } from "@/lib/types";
import type { Session } from "@/lib/types";

export function SessionTable({
  sessions,
  now,
  showOperator = false,
  emptyMessage = "No sessions yet.",
}: {
  sessions: Session[] | null;
  now: number;
  /** on the approver tab the operator is the interesting column */
  showOperator?: boolean;
  emptyMessage?: string;
}) {
  const router = useRouter();

  if (sessions === null) {
    return (
      <div className="space-y-2 p-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-8 w-full" />
        ))}
      </div>
    );
  }
  if (sessions.length === 0) {
    return <p className="px-4 py-10 text-center text-[13px] text-muted-foreground">{emptyMessage}</p>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Session</TableHead>
          <TableHead>Agent</TableHead>
          <TableHead>State</TableHead>
          {showOperator && <TableHead>Operator</TableHead>}
          <TableHead className="text-right">Pending</TableHead>
          <TableHead className="text-right">Journal</TableHead>
          <TableHead className="text-right">Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sessions.map((s) => (
          <TableRow
            key={s.session_id}
            className="cursor-pointer"
            onClick={() => router.push(`/session?sid=${s.session_id}`)}
          >
            <TableCell title={s.session_id}>
              <span className="font-mono text-xs">{shortId(s.session_id)}</span>
            </TableCell>
            <TableCell className="font-mono text-xs">
              <Link
                href={`/agent?id=${encodeURIComponent(s.agent_id)}`}
                onClick={(e) => e.stopPropagation()}
                className="hover:underline"
              >
                {s.agent_id}
              </Link>
              <span className="text-faint"> v{s.agent_version}</span>
            </TableCell>
            <TableCell>
              <StateBadge state={sessionState(s)} />
            </TableCell>
            {showOperator && (
              <TableCell className="font-mono text-xs text-muted-foreground">{s.operator}</TableCell>
            )}
            <TableCell className="text-right font-mono text-xs tabular-nums">
              {s.pending.length ? <span className="text-warn">{s.pending.length}</span> : "—"}
            </TableCell>
            <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
              {s.last_event_seq}
            </TableCell>
            <TableCell className="text-right font-mono text-xs text-muted-foreground">
              {relTime(epoch(s.updated_at), now) || "—"}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
