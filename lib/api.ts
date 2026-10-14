import type { ApiHealth, AuthTokenResponse, AuthUser } from "@/lib/types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

// The access token lives in sessionStorage: it survives a page reload but
// not closing the tab/browser, which bounds how long a token sitting in
// browser storage can be lifted by anything that runs in the page. (An
// httpOnly cookie would remove that exposure entirely, but needs
// cookie-based auth plus CSRF protection on the API — a larger, separate
// change than Passage 4 §6.2's "Auth: required (JWT)" asks for.)
const TOKEN_KEY = "oxygen-ai.accessToken";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage disabled (private mode, blocked cookies)
  }
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Nothing useful to do — the user simply has to sign in again.
  }
}

// Thrown when a request that carried a token comes back 401: the token
// expired, was revoked by logout, or the account was disabled. The stored
// token is already cleared by the time this is thrown.
export class SessionExpiredError extends Error {
  constructor() {
    super("Your session has expired — please sign in again.");
    this.name = "SessionExpiredError";
  }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (res.status === 401 && token) {
    setToken(null);
    throw new SessionExpiredError();
  }
  return res;
}

async function errorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = await res.json();
    return (typeof data?.error === "string" && data.error) || fallback;
  } catch {
    return fallback;
  }
}

export async function fetchHealth(): Promise<ApiHealth> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error(`API health check failed (${res.status})`);
  return res.json();
}

export async function fetchMe(): Promise<AuthUser | null> {
  if (!getToken()) return null;
  try {
    const res = await apiFetch("/api/auth/me");
    return res.ok ? ((await res.json()) as AuthUser) : null;
  } catch (err) {
    if (err instanceof SessionExpiredError) return null;
    throw err;
  }
}

export async function authenticate(
  kind: "login" | "register",
  email: string,
  password: string
): Promise<AuthUser> {
  const res = await fetch(`${API_BASE}/api/auth/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    throw new Error(await errorMessage(res, kind === "login" ? "Sign-in failed" : "Registration failed"));
  }
  const data = (await res.json()) as AuthTokenResponse;
  setToken(data.accessToken);
  return data.user;
}

export async function logout(): Promise<void> {
  try {
    // Revokes the session server-side so the token stops working
    // immediately, not at its natural expiry. Best effort: the local token
    // is dropped regardless.
    await apiFetch("/api/auth/logout", { method: "POST" });
  } catch {
    // SessionExpiredError (already revoked) or a network failure — either
    // way the user is signed out locally below.
  } finally {
    setToken(null);
  }
}
