"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAgents, useNow } from "@/lib/hooks";
import { relTime } from "@/lib/format";
import { epoch } from "@/lib/types";

export default function AgentsPage() {
  const agents = useAgents();
  const now = useNow();
  const router = useRouter();

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 sm:p-6">
      <div>
        <h1 className="font-serif text-2xl tracking-tight">Agents</h1>
        <p className="pt-1 text-[12px] text-muted-foreground">
          What is published. Definitions live in Git and reach here through CI as the admin group; enabling and
          disabling is <code className="font-mono text-xs">milos agents enable|disable</code>.
        </p>
      </div>
      <Card>
        <CardContent className="px-2 py-2">
          {agents === null ? (
            <div className="space-y-2 p-2">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : agents.length === 0 ? (
            <p className="p-10 text-center text-[13px] text-muted-foreground">
              No agents published yet — <code className="font-mono text-xs">milos agents publish</code> as the admin
              group.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead>Purpose</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Needs approval</TableHead>
                  <TableHead className="text-right">Published</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {agents.map(({ agent, version }) => {
                  const href = `/agent?id=${encodeURIComponent(agent.agent_id)}`;
                  return (
                    <TableRow key={agent.agent_id} className="cursor-pointer" onClick={() => router.push(href)}>
                      <TableCell className="font-mono text-[13px] font-medium">
                        <Link href={href} onClick={(e) => e.stopPropagation()} className="hover:underline">
                          {agent.agent_id}
                        </Link>
                        <span className="text-faint"> v{agent.latest_version}</span>
                        {!agent.enabled && <Badge className="ml-2 text-destructive">disabled</Badge>}
                      </TableCell>
                      <TableCell className="max-w-[22rem] truncate text-xs text-muted-foreground">
                        {version.purpose}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">{version.owner}</TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">{version.model}</TableCell>
                      <TableCell className="max-w-[18rem] truncate font-mono text-[11px] text-muted-foreground">
                        {version.approval_required.join(" ") || "—"}
                      </TableCell>
                      <TableCell className="text-right text-xs text-muted-foreground">
                        {relTime(epoch(version.published_at), now)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
