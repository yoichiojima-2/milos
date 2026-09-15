// Thin fetch wrapper over /v1 on the page's own origin: the IAP cookie that
// admitted the browser is the identity for every call. Tracks connectivity
// and the server clock offset (from /v1/me) so approval countdowns render
// against the API's clock rather than the browser's.

import type { ApiError } from "./types";

let clockOffset = 0; // serverNow - localNow, seconds
let onlineListener: ((online: boolean) => void) | null = null;

export function setOnlineListener(listener: ((online: boolean) => void) | null): void {
  onlineListener = listener;
}

export function serverNow(): number {
  return Date.now() / 1000 + clockOffset;
}

export function syncClock(serverIso: string): void {
  const ms = Date.parse(serverIso);
  if (!Number.isNaN(ms)) clockOffset = ms / 1000 - Date.now() / 1000;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      // IAP answers an expired session with 401 instead of a login redirect
      // when the request says it is XHR; the reload below then re-signs in.
      headers: { "X-Requested-With": "XMLHttpRequest", ...(init?.headers ?? {}) },
    });
  } catch (err) {
    onlineListener?.(false);
    throw err;
  }
  onlineListener?.(true);
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (response.status === 401 && data === null) {
    // Not the API's own 401 (that is JSON): the IAP session expired.
    window.location.reload();
    throw new Error("signing in again…");
  }
  if (!response.ok) throw new Error(errorMessage(response.status, data));
  return data as T;
}

function errorMessage(status: number, data: unknown): string {
  if (data && typeof data === "object") {
    const body = data as Partial<ApiError> & { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    // FastAPI validation errors: a list of {loc, msg}
    if (Array.isArray(body.detail)) {
      const problems = body.detail as { loc?: unknown[]; msg?: string }[];
      return problems.map((d) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg ?? ""}`).join("; ");
    }
    if (typeof body.error === "string") return body.error;
  }
  return String(status);
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
