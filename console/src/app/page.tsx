"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { SessionForm } from "@/components/session-form";
import { SessionTable } from "@/components/session-table";
import { useNow, useSessions } from "@/lib/hooks";
import type { SessionRole } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const TABS: [SessionRole, string, string][] = [
  ["operator", "Mine", "sessions you started"],
  ["approver", "Approving", "sessions that name you as an approver"],
];

export default function SessionsPage() {
  const [role, setRole] = useState<SessionRole>("operator");
  const { sessions } = useSessions(role);
  const now = useNow();
  const router = useRouter();
  const [creating, setCreating] = useState(false);

  return (
    <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-serif text-2xl tracking-tight">Sessions</h1>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex w-fit items-center gap-1 rounded-lg bg-secondary p-0.5" role="tablist">
            {TABS.map(([tab, label, hint]) => (
              <button
                key={tab}
                type="button"
                role="tab"
                title={hint}
                aria-selected={role === tab}
                onClick={() => setRole(tab)}
                className={cn(
                  "rounded-md px-3 py-1 text-[12px] font-medium transition-colors",
                  role === tab ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          {!creating && (
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus />
              New session
            </Button>
          )}
        </div>
      </div>
      {creating && (
        <SessionForm
          onCancel={() => setCreating(false)}
          onCreated={(sid) => {
            setCreating(false);
            router.push(`/session?sid=${sid}`);
          }}
        />
      )}
      <Card>
        <CardContent className="px-2 py-2">
          <SessionTable
            sessions={sessions}
            now={now}
            showOperator={role === "approver"}
            emptyMessage={
              role === "operator"
                ? "No sessions yet — start one with New session, or `milos run` from the CLI."
                : "No session names you as an approver."
            }
          />
        </CardContent>
      </Card>
    </div>
  );
}
