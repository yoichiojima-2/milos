"use client";

import { useEffect, useRef, useState } from "react";
import { api, serverNow, setOnlineListener, syncClock } from "./api";
import type { Event, Me, PendingRequest, Published, Session } from "./types";

/** Ticker on the server's clock (see api.ts) driving countdowns and relative times. */
export function useNow(intervalMs = 500): number {
  const [now, setNow] = useState(serverNow);
  useEffect(() => {
    const timer = setInterval(() => setNow(serverNow()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Connectivity flag fed by the fetch wrapper. One consumer (the app shell). */
export function useOnline(): boolean {
  const [online, setOnline] = useState(true);
  useEffect(() => {
    setOnlineListener(setOnline);
    return () => setOnlineListener(null);
  }, []);
  return online;
}

// `deps` re-runs the effect when they change — a refresh nonce to pull fresh
// data without waiting out the interval, or the id the poll closes over. The
// poll gets an `alive` check so a response landing after the deps moved on
// (or the component unmounted) can be dropped instead of applied.
function usePolling(poll: (alive: () => boolean) => void, intervalMs: number, deps: readonly unknown[] = []) {
  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (!document.hidden) poll(() => alive);
    };
    tick();
    const timer = setInterval(tick, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);
}

/** The signed-in user, and the clock sync that rides along with it. */
export function useMe(intervalMs = 60000): Me | null {
  const [me, setMe] = useState<Me | null>(null);
  usePolling((alive) => {
    api<Me>("/v1/me")
      .then((data) => {
        syncClock(data.now);
        if (alive()) setMe(data);
      })
      .catch(() => {});
  }, intervalMs);
  return me;
}

export type SessionRole = "operator" | "approver";

export function useSessions(role: SessionRole, intervalMs = 4000): { sessions: Session[] | null; refresh: () => void } {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => setSessions(null), [role]);
  usePolling(
    (alive) => {
      api<Session[]>(`/v1/sessions?role=${role}`)
        .then((data) => {
          if (alive()) setSessions(data);
        })
        .catch(() => {});
    },
    intervalMs,
    [role, nonce],
  );
  return { sessions, refresh: () => setNonce((n) => n + 1) };
}

export function useAgents(intervalMs = 10000): Published[] | null {
  const [agents, setAgents] = useState<Published[] | null>(null);
  usePolling((alive) => {
    api<Published[]>("/v1/agents")
      .then((data) => {
        if (alive()) setAgents(data);
      })
      .catch(() => {});
  }, intervalMs);
  return agents;
}

export function useAgent(agentId: string | null, intervalMs = 10000): { agent: Published | null; missing: boolean } {
  const [agent, setAgent] = useState<Published | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    setAgent(null);
    setMissing(false);
  }, [agentId]);
  usePolling(
    (alive) => {
      if (!agentId) return;
      api<Published>(`/v1/agents/${encodeURIComponent(agentId)}`)
        .then((data) => {
          if (alive()) setAgent(data);
        })
        .catch((err: Error) => {
          if (alive() && /not found|no such|unknown/i.test(err.message)) setMissing(true);
        });
    },
    intervalMs,
    [agentId],
  );
  return { agent, missing };
}

export interface SessionFeed {
  session: Session | null;
  events: Event[];
  /** The calls the session is parked on, each with the journal entry carrying its arguments. */
  pending: PendingRequest[];
  missing: boolean;
  refresh: () => void;
}

/** Poll of the focused session: the session document plus an append-only
 *  event cursor (`GET /events?after=seq`), the same loop `milos run` uses. */
export function useSessionFeed(sid: string | null, intervalMs = 1500): SessionFeed {
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [missing, setMissing] = useState(false);
  const [nonce, setNonce] = useState(0);
  const cursorRef = useRef(0);

  useEffect(() => {
    setSession(null);
    setEvents([]);
    setMissing(false);
    cursorRef.current = 0;
  }, [sid]);

  usePolling(
    async (alive) => {
      if (!sid) return;
      try {
        const [next, fresh] = await Promise.all([
          api<Session>(`/v1/sessions/${sid}`),
          api<Event[]>(`/v1/sessions/${sid}/events?after=${cursorRef.current}`),
        ]);
        if (!alive()) return;
        setSession(next);
        if (fresh.length) {
          cursorRef.current = fresh[fresh.length - 1].seq;
          setEvents((prev) => [...prev, ...fresh]);
        }
      } catch (err) {
        if (alive() && /not found|no such/i.test((err as Error).message)) setMissing(true);
      }
    },
    intervalMs,
    [sid, nonce],
  );

  const pending = session ? pendingRequests(session, events) : [];
  return { session, events, pending, missing, refresh: () => setNonce((n) => n + 1) };
}

function pendingRequests(session: Session, events: Event[]): PendingRequest[] {
  const byId = new Map(events.filter((e) => e.type === "agent.tool_use").map((e) => [e.tool_use_id, e]));
  return session.pending.map((call) => ({ session, call, request: byId.get(call.tool_use_id) ?? null }));
}

/** Every call waiting on the signed-in user: sessions that name them as an
 *  approver and are parked. The arguments live in each session's journal, so
 *  one events fetch per parked session rides along (cached by tool use id). */
export function useInbox(intervalMs = 3000): { inbox: PendingRequest[] | null; dismiss: (toolUseId: string) => void } {
  const [inbox, setInbox] = useState<PendingRequest[] | null>(null);
  const requestsRef = useRef(new Map<string, Event>()); // tool_use_id → agent.tool_use event
  usePolling(async (alive) => {
    try {
      const sessions = await api<Session[]>("/v1/sessions?role=approver");
      const parked = sessions.filter((s) => s.pending.length > 0 && s.status !== "terminated");
      const items: PendingRequest[] = [];
      for (const session of parked) {
        const unknown = session.pending.filter((c) => !requestsRef.current.has(c.tool_use_id));
        if (unknown.length) {
          const events = await api<Event[]>(`/v1/sessions/${session.session_id}/events`);
          for (const e of events) if (e.type === "agent.tool_use" && e.tool_use_id) requestsRef.current.set(e.tool_use_id, e);
        }
        for (const call of session.pending)
          items.push({ session, call, request: requestsRef.current.get(call.tool_use_id) ?? null });
      }
      if (alive()) setInbox((prev) => (prev && sameInbox(prev, items) ? prev : items));
    } catch {
      // connectivity is surfaced by the shell indicator
    }
  }, intervalMs);
  const dismiss = (toolUseId: string) =>
    setInbox((prev) => prev?.filter((p) => p.call.tool_use_id !== toolUseId) ?? prev);
  return { inbox, dismiss };
}

// Keep the previous array (and skip a re-render of the countdown cards) when
// the pending set hasn't changed.
function sameInbox(a: PendingRequest[], b: PendingRequest[]): boolean {
  const key = (p: PendingRequest) => `${p.session.session_id}/${p.call.tool_use_id}/${p.request ? 1 : 0}`;
  return a.map(key).join(",") === b.map(key).join(",");
}

/** Transient status line; clears itself after 4s. */
export function useFlash(): [string, (text: string) => void] {
  const [flash, setFlash] = useState("");
  const show = (text: string) => {
    setFlash(text);
    if (text) setTimeout(() => setFlash((cur) => (cur === text ? "" : cur)), 4000);
  };
  return [flash, show];
}

/** useFlash plus the try/catch every action shares: flash the message the
 * action returns on success, flash the error on failure. */
export function useAction(): [string, (fn: () => Promise<string | void>) => Promise<void>] {
  const [flash, showFlash] = useFlash();
  const run = async (fn: () => Promise<string | void>) => {
    try {
      const message = await fn();
      if (message) showFlash(message);
    } catch (err) {
      showFlash(`error: ${(err as Error).message}`);
    }
  };
  return [flash, run];
}
