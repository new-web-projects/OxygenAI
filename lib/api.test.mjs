// Tests for lib/api.ts — the browser-side half of Passage 4 §6.2's "Auth:
// required (JWT)". Run with: npm run test:client  (Node >= 22.6).
//
// The repo has no JS test framework, so this uses Node's built-in runner
// and executes the real TypeScript module (type annotations stripped by
// Node) with `fetch` and `sessionStorage` replaced by in-memory stubs.
// It does not exercise a browser — see the Phase 1 report for what was and
// was not run.

import assert from "node:assert/strict";
import { beforeEach, describe, test } from "node:test";

const store = new Map();
globalThis.window = {
  sessionStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => void store.set(k, String(v)),
    removeItem: (k) => void store.delete(k),
  },
};

let calls = [];
let responder = () => new Response("{}", { status: 200 });
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url: String(url), init, headers: new Headers(init.headers) });
  return responder(url, init);
};

const api = await import("./api.ts");

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  store.clear();
  calls = [];
  responder = () => json(200, {});
});

describe("apiFetch", () => {
  test("attaches the bearer token and a JSON content type when there is a body", async () => {
    api.setToken("tok-123");
    await api.apiFetch("/api/ai/analyze", { method: "POST", body: "{}" });
    assert.equal(calls[0].url, "http://localhost:8000/api/ai/analyze");
    assert.equal(calls[0].headers.get("Authorization"), "Bearer tok-123");
    assert.equal(calls[0].headers.get("Content-Type"), "application/json");
  });

  test("sends no Authorization header when signed out", async () => {
    await api.apiFetch("/api/ai/providers");
    assert.equal(calls[0].headers.get("Authorization"), null);
  });

  test("a 401 on a request that carried a token clears it and throws SessionExpiredError", async () => {
    api.setToken("expired-token");
    responder = () => json(401, { error: "Invalid, expired, or revoked token" });
    await assert.rejects(() => api.apiFetch("/api/ai/analyze", { method: "POST", body: "{}" }), api.SessionExpiredError);
    assert.equal(api.getToken(), null);
  });

  test("a 401 with no token is returned normally (it is a failed login, not an expired session)", async () => {
    responder = () => json(401, { error: "Incorrect email or password" });
    const res = await api.apiFetch("/api/ai/analyze", { method: "POST", body: "{}" });
    assert.equal(res.status, 401);
  });

  test("other error statuses pass through untouched and keep the token", async () => {
    api.setToken("still-good");
    responder = () => json(429, { error: "Too many requests" });
    const res = await api.apiFetch("/api/ai/analyze", { method: "POST", body: "{}" });
    assert.equal(res.status, 429);
    assert.equal(api.getToken(), "still-good");
  });
});

describe("authenticate", () => {
  const user = { id: "u1", email: "a@example.com", role: "user", status: "active" };

  test("stores the token and returns the user on success", async () => {
    responder = () => json(200, { accessToken: "fresh", tokenType: "bearer", expiresIn: 3600, user });
    const result = await api.authenticate("login", "a@example.com", "correct-horse-battery");
    assert.deepEqual(result, user);
    assert.equal(api.getToken(), "fresh");
    assert.equal(calls[0].url, "http://localhost:8000/api/auth/login");
    assert.deepEqual(JSON.parse(calls[0].init.body), { email: "a@example.com", password: "correct-horse-battery" });
  });

  test("surfaces the server's message and stores nothing on failure", async () => {
    responder = () => json(401, { error: "Incorrect email or password" });
    await assert.rejects(() => api.authenticate("login", "a@example.com", "wrong"), /Incorrect email or password/);
    assert.equal(api.getToken(), null);
  });

  test("falls back to a generic message when the error body is not JSON", async () => {
    responder = () => new Response("<html>bad gateway</html>", { status: 502 });
    await assert.rejects(() => api.authenticate("register", "a@example.com", "x"), /Registration failed/);
  });
});

describe("logout and fetchMe", () => {
  test("logout revokes server-side and always drops the local token", async () => {
    api.setToken("to-revoke");
    await api.logout();
    assert.equal(calls[0].url, "http://localhost:8000/api/auth/logout");
    assert.equal(calls[0].headers.get("Authorization"), "Bearer to-revoke");
    assert.equal(api.getToken(), null);
  });

  test("logout still clears the token when the network call fails", async () => {
    api.setToken("to-revoke");
    responder = () => {
      throw new TypeError("network down");
    };
    await api.logout();
    assert.equal(api.getToken(), null);
  });

  test("fetchMe makes no request without a token", async () => {
    assert.equal(await api.fetchMe(), null);
    assert.equal(calls.length, 0);
  });

  test("fetchMe returns null (and clears the token) for a rejected token", async () => {
    api.setToken("revoked");
    responder = () => json(401, { error: "Invalid, expired, or revoked token" });
    assert.equal(await api.fetchMe(), null);
    assert.equal(api.getToken(), null);
  });

  test("fetchMe returns the user for a good token", async () => {
    api.setToken("good");
    const user = { id: "u1", email: "a@example.com", role: "admin", status: "active" };
    responder = () => json(200, user);
    assert.deepEqual(await api.fetchMe(), user);
  });
});
