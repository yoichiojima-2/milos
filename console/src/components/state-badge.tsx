import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { SessionState } from "@/lib/types";

// State is never carried by color alone: the badge always pairs dot + label.
export const STATE_DOT: Record<SessionState, string> = {
  running: "bg-ok animate-pulse-dot",
  rescheduling: "bg-info animate-pulse-dot",
  waiting: "bg-warn-dot animate-pulse-dot",
  idle: "bg-faint",
  budget: "bg-warn-dot",
  stopped: "bg-faint",
  attention: "bg-destructive",
  terminated: "bg-destructive",
  unknown: "bg-faint",
};

const LABEL: Record<SessionState, string> = {
  running: "running",
  rescheduling: "rescheduling",
  waiting: "needs approval",
  idle: "idle",
  budget: "budget reached",
  stopped: "stopped",
  attention: "needs attention",
  terminated: "terminated",
  unknown: "unknown",
};

export function StateBadge({ state, className }: { state: SessionState; className?: string }) {
  return (
    <Badge className={className}>
      <span className={cn("size-[7px] rounded-full", STATE_DOT[state] || "bg-faint")} />
      {LABEL[state]}
    </Badge>
  );
}
