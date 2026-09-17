"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { SessionForm } from "@/components/session-form";
import { useAgent } from "@/lib/hooks";
import { clockTime } from "@/lib/format";
import { epoch } from "@/lib/types";

export default function AgentPage() {
  return (
    <Suspense>
      <AgentInner />
    </Suspense>
  );
}

function AgentInner() {
  const id = useSearchParams().get("id");
  const { agent, missing } = useAgent(id);
  const router = useRouter();
  const [creating, setCreating] = useState(false);

  if (!id)
    return (
      <p className="pt-20 text-center text-[13px] text-muted-foreground">
        No agent selected — pick one from the Agents page.
      </p>
    );
  if (missing)
    return <p className="pt-20 text-center text-[13px] text-muted-foreground">No agent {id} is published.</p>;
  if (!agent)
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full max-w-3xl" />
      </div>
    );

  const v = agent.version;
  const rows: [string, string][] = [
    ["purpose", v.purpose],
    ["owner", v.owner],
    ["allowed groups", v.allowed_groups.join(", ")],
    ["data classes", v.data_classes.join(", ")],
    ["allowed tools", v.allowed_tools.join(", ")],
    ["needs approval", v.approval_required.join(", ") || "—"],
    ["approval ttl", `${v.approval_ttl_sec}s`],
    ["limits", `${v.max_turns} turns · $${v.max_budget_usd} · ${v.max_concurrent_sessions} concurrent`],
    ["model", v.model],
    ["connectors", v.connectors.join(", ") || "—"],
    ["bigquery", [...v.datasets, ...(v.workspace ? [`agent_${v.agent_id.replace(/-/g, "_")} (workspace)`] : [])].join(", ") || "—"],
    ["runner identity", v.runner_sa],
    ["definition sha256", v.definition_sha256],
    ["published", clockTime(epoch(v.published_at))],
  ];

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="flex items-center gap-2 font-serif text-2xl tracking-tight">
          <span className="font-mono">{agent.agent.agent_id}</span>
          <span className="text-base text-muted-foreground">v{agent.agent.latest_version}</span>
          {!agent.agent.enabled && <Badge className="text-destructive">disabled</Badge>}
        </h1>
        {!creating && agent.agent.enabled && (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus />
            New session
          </Button>
        )}
      </div>
      {creating && (
        <SessionForm
          agent={agent.agent.agent_id}
          onCancel={() => setCreating(false)}
          onCreated={(sid) => router.push(`/session?sid=${sid}`)}
        />
      )}
      <Card className="max-w-3xl">
        <CardHeader>
          <CardTitle>Definition</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-x-4 gap-y-1.5 text-[12px] sm:grid-cols-[auto_1fr]">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="tracking-[0.08em] text-faint uppercase">{label}</dt>
                <dd className="font-mono break-all text-muted-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
      {v.system_prompt && (
        <Card className="max-w-3xl">
          <CardHeader>
            <CardTitle>System prompt</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="font-mono text-xs whitespace-pre-wrap text-muted-foreground [overflow-wrap:break-word]">
              {v.system_prompt}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
