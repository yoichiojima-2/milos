"use client";

import { useLayoutEffect, useRef } from "react";
import type { Event } from "@/lib/types";
import { compact, cost } from "@/lib/format";
import { renderMarkdown } from "@/lib/markdown";
import { cn } from "@/lib/utils";

// Renders the journal (models.EventType) as a chat: the operator's messages in
// a tinted bubble on the right, the agent's answers as prose, and every tool
// call, permission, result and status change folded into a muted row — the
// audit trail reads inline without competing with the conversation.

function CollapsibleRow({
  className,
  summary,
  detail,
}: {
  className?: string;
  summary: React.ReactNode;
  detail: string;
}) {
  return (
    <details className={cn("group font-mono text-xs", className)}>
      <summary className="-mx-2 overflow-hidden rounded-lg px-2 py-1 text-ellipsis whitespace-nowrap text-muted-foreground transition-colors hover:bg-secondary/60">
        {summary}
      </summary>
      <pre className="mt-1.5 overflow-x-auto rounded-xl border border-border bg-surface px-3 py-2.5 whitespace-pre-wrap [overflow-wrap:break-word]">
        {detail}
      </pre>
    </details>
  );
}

function Chip({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("font-mono text-[11px] text-faint", className)}>{children}</div>;
}

const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));

function EventView({ event }: { event: Event }) {
  const p = event.payload;
  switch (event.type) {
    case "user.message":
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl bg-secondary px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap [overflow-wrap:break-word]">
            {str(p.text)}
          </div>
        </div>
      );
    case "agent.message":
      return (
        <div
          className="chat-prose text-[15px] leading-7 [overflow-wrap:break-word]"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(str(p.text)) }}
        />
      );
    case "agent.tool_use": {
      const outcome = str(p.outcome);
      const denied = outcome === "deny" || outcome === "stop";
      return (
        <CollapsibleRow
          className={denied ? "[&>summary]:text-destructive" : ""}
          summary={
            <>
              <b className="font-semibold text-foreground">{str(p.tool_name) || "?"}</b> {compact(p.args ?? {}, 120)}
              <span className={cn("ml-2", outcome === "require_approval" ? "text-warn" : "")}>
                → {outcome}
              </span>
            </>
          }
          detail={`${JSON.stringify(p.args ?? {}, null, 2)}\n\n${outcome}: ${str(p.reason)}`}
        />
      );
    }
    case "tool.permitted":
      return (
        <Chip>
          permitted {str(p.tool_name)} · {str(p.reason)}
        </Chip>
      );
    case "tool.result": {
      const failed = p.outcome === "failed";
      return (
        <CollapsibleRow
          className={failed ? "[&>summary]:text-destructive" : ""}
          summary={`↳ ${str(p.outcome)} ${compact(str(p.summary), 140)}`}
          detail={str(p.summary)}
        />
      );
    }
    case "user.approval":
      return (
        <Chip className={p.verdict === "deny" ? "text-destructive" : "text-ok"}>
          {str(p.verdict)} {str(p.tool_name)} · by {event.actor}
          {p.timed_out ? " · timed out" : ""}
        </Chip>
      );
    case "user.interrupt":
      return <Chip>— interrupted by {event.actor} —</Chip>;
    case "session.status":
      return (
        <Chip>
          — {str(p.status)}
          {p.stop_reason ? ` · ${str(p.stop_reason)}` : ""} —
        </Chip>
      );
    case "session.usage": {
      const parts = [`${str(p.subtype) || "turn"} · ${str(p.num_turns) || 0} turns`];
      if (typeof p.total_cost_usd === "number") parts.push(cost(p.total_cost_usd));
      if (typeof p.duration_ms === "number") parts.push(`${(p.duration_ms / 1000).toFixed(1)}s`);
      return (
        <div className="flex items-center gap-3 pt-1">
          <span className="h-px flex-1 bg-border" />
          <span className="font-mono text-[11px] text-faint">{parts.join(" · ")}</span>
          <span className="h-px flex-1 bg-border" />
        </div>
      );
    }
    default:
      return <Chip>{event.type}</Chip>;
  }
}

export function Transcript({
  events,
  placeholder,
  working = false,
}: {
  events: Event[];
  placeholder: string | null;
  /** True while the agent owes a response — renders a typing indicator. */
  working?: boolean;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  // Follow new events only while the reader is at the bottom; once they
  // scroll up to read, stop yanking the view down on every poll.
  const pinnedRef = useRef(true);

  useLayoutEffect(() => {
    const box = boxRef.current;
    if (box && pinnedRef.current) box.scrollTop = box.scrollHeight;
  }, [events, working]);

  return (
    <div
      ref={boxRef}
      onScroll={() => {
        const box = boxRef.current!;
        pinnedRef.current = box.scrollTop + box.clientHeight >= box.scrollHeight - 48;
      }}
      className="min-h-0 flex-1 overflow-y-auto px-5 py-6"
    >
      <div className="mx-auto max-w-3xl space-y-5">
        {placeholder !== null && events.length === 0 && (
          <p className="pt-16 text-center text-[13px] text-muted-foreground">{placeholder}</p>
        )}
        {events.map((event) => (
          <EventView key={event.event_id} event={event} />
        ))}
        {working && (
          <div className="flex items-center gap-1.5 py-1" aria-label="agent is working">
            {[0, 200, 400].map((delay) => (
              <span
                key={delay}
                className="size-1.5 animate-pulse rounded-full bg-muted-foreground"
                style={{ animationDelay: `${delay}ms` }}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
