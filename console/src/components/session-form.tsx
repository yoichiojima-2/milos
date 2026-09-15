"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useAgents } from "@/lib/hooks";
import { post } from "@/lib/api";
import type { Session } from "@/lib/types";

/** New-session form: an agent, its first message, and optionally who may
 *  approve and who may watch. Everything else comes from the published
 *  definition — a session is `POST /v1/sessions`, nothing more. */
export function SessionForm({
  onCreated,
  onCancel,
  agent: pinned,
}: {
  onCreated: (sessionId: string) => void;
  onCancel: () => void;
  /** pins the agent (the agent page), leaving only the message to write */
  agent?: string;
}) {
  const agents = useAgents();
  const [agent, setAgent] = useState(pinned ?? "");
  const [message, setMessage] = useState("");
  const [approvers, setApprovers] = useState("");
  const [viewers, setViewers] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const enabled = (agents ?? []).filter((p) => p.agent.enabled);
  const chosen = enabled.find((p) => p.agent.agent_id === agent) ?? null;
  const ready = message.trim() && agent.trim();
  const list = (text: string) =>
    text
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setError("");
    try {
      const session = await post<Session>("/v1/sessions", {
        agent_id: agent.trim(),
        message: message.trim(),
        approvers: list(approvers),
        viewers: list(viewers),
      });
      onCreated(session.session_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{pinned ? `New session as ${pinned}` : "New session"}</CardTitle>
        <CardDescription>
          Starts a runner now. Follow-up messages go to the same session from its journal.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">Message</span>
            <Textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit();
              }}
              rows={3}
              required
              autoFocus
              placeholder="Summarise the weekly numbers into a short report."
              className="rounded-lg border border-input bg-card px-3 py-2 text-[13px] leading-relaxed"
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-3">
            {!pinned && (
              <label className="block space-y-1.5">
                <span className="text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
                  Agent
                </span>
                <Select value={agent} onChange={(e) => setAgent(e.target.value)} required>
                  <option value="">pick one…</option>
                  {enabled.map((p) => (
                    <option key={p.agent.agent_id} value={p.agent.agent_id}>
                      {p.agent.agent_id} v{p.agent.latest_version}
                    </option>
                  ))}
                </Select>
              </label>
            )}
            <label className="block space-y-1.5">
              <span className="text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
                Approvers
              </span>
              <Input
                value={approvers}
                onChange={(e) => setApprovers(e.target.value)}
                placeholder="empty: anyone in the agent's groups"
                className="font-mono text-xs"
              />
            </label>
            <label className="block space-y-1.5">
              <span className="text-[11px] font-medium tracking-[0.08em] text-muted-foreground uppercase">
                Viewers
              </span>
              <Input
                value={viewers}
                onChange={(e) => setViewers(e.target.value)}
                placeholder="emails, comma separated"
                className="font-mono text-xs"
              />
            </label>
          </div>
          {chosen && (
            <dl className="grid gap-x-4 gap-y-1 rounded-lg border border-border bg-secondary/40 p-3 text-[11px] sm:grid-cols-[auto_1fr]">
              {[
                ["purpose", chosen.version.purpose],
                ["model", chosen.version.model],
                ["tools", chosen.version.allowed_tools.join(", ")],
                ["needs approval", chosen.version.approval_required.join(", ") || "—"],
                ["limits", `${chosen.version.max_turns} turns · $${chosen.version.max_budget_usd}`],
              ].map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="tracking-[0.08em] text-faint uppercase">{label}</dt>
                  <dd className="font-mono break-words text-muted-foreground">{value}</dd>
                </div>
              ))}
            </dl>
          )}
          {error && <p className="text-[12px] text-destructive">{error}</p>}
          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" disabled={busy || !ready}>
              {busy ? "Starting…" : "Start session"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <span className="text-[11px] text-muted-foreground">
              Calls in the definition&apos;s approval list pause the session for a person.
            </span>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
