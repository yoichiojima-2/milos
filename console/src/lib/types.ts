// TypeScript mirror of the wire types in src/milos/models.py — what /v1
// accepts and returns. Datetimes arrive as ISO strings; `epoch` turns them
// into the seconds the countdowns and relative times work in.

export type SessionStatus = "idle" | "running" | "rescheduling" | "terminated";
export type StopReason = "end_turn" | "requires_action" | "budget_reached" | "stopped" | "needs_attention";
export type Verdict = "allow" | "deny";
export type Outcome = "allow" | "deny" | "require_approval" | "stop";

export interface PendingCall {
  tool_use_id: string;
  tool_name: string;
  args_sha256: string;
  event_id: string;
}

export interface LeaseView {
  runner_id: string;
  last_poll_at: string;
}

/** `SessionView`: a session as the public API returns it (no lease token). */
export interface Session {
  session_id: string;
  agent_id: string;
  agent_version: number;
  definition_sha256: string;
  status: SessionStatus;
  stop_reason: StopReason | null;
  operator: string;
  viewers: string[];
  approvers: string[];
  lease: LeaseView | null;
  snapshot: number;
  consumed_seq: number;
  last_message_seq: number;
  pending: PendingCall[];
  approval_expires_at: string | null;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
}

export type EventType =
  | "user.message"
  | "user.interrupt"
  | "user.approval"
  | "agent.message"
  | "agent.tool_use"
  | "tool.permitted"
  | "tool.result"
  | "session.usage"
  | "session.status";

export interface Event {
  event_id: string;
  seq: number;
  type: EventType;
  actor: string;
  tool_use_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Agent {
  agent_id: string;
  enabled: boolean;
  latest_version: number;
}

export interface AgentVersion {
  agent_id: string;
  version: number;
  definition_sha256: string;
  purpose: string;
  owner: string;
  allowed_groups: string[];
  data_classes: string[];
  allowed_tools: string[];
  approval_required: string[];
  approval_ttl_sec: number;
  max_turns: number;
  max_budget_usd: number;
  max_concurrent_sessions: number;
  model: string;
  runner_sa: string;
  system_prompt: string;
  connectors: string[];
  published_at: string;
}

export interface Published {
  agent: Agent;
  version: AgentVersion;
}

export interface Approval {
  tool_use_id: string;
  verdict: Verdict;
  decided_by: string;
  decided_at: string;
  expires_at: string;
  timed_out: boolean;
  tool_name: string;
  args_sha256: string;
}

export interface Me {
  email: string;
  admin: boolean;
  now: string;
}

export interface ApiError {
  error: string;
  detail: string;
}

/** A parked tool call together with the journal entry that carries its arguments. */
export interface PendingRequest {
  session: Session;
  call: PendingCall;
  request: Event | null;
}

// --- derived --------------------------------------------------------------

/** Status and stop reason folded into one word for badges and filters. */
export type SessionState =
  | "running"
  | "rescheduling"
  | "waiting"
  | "idle"
  | "budget"
  | "stopped"
  | "attention"
  | "terminated"
  | "unknown";

export const ACTIVE_STATES: ReadonlySet<SessionState> = new Set(["running", "rescheduling", "waiting"]);

export function sessionState(s: Session): SessionState {
  switch (s.status) {
    case "terminated":
      return "terminated";
    case "running":
      return "running";
    case "rescheduling":
      return "rescheduling";
    case "idle":
      switch (s.stop_reason) {
        case "requires_action":
          return "waiting";
        case "end_turn":
          return "idle";
        case "budget_reached":
          return "budget";
        case "stopped":
          return "stopped";
        case "needs_attention":
          return "attention";
        default:
          return "unknown";
      }
  }
}

export function epoch(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** Who may decide a parked call: anyone but the operator, or only the named approvers (access.can_decide). */
export function canDecide(session: Session, email: string): boolean {
  if (email === session.operator) return false;
  return session.approvers.length === 0 || session.approvers.includes(email);
}

export function canOperate(session: Session, email: string): boolean {
  return email === session.operator;
}
