"use client";

import { useState, type FormEvent } from "react";
import { authenticate } from "@/lib/api";
import type { ApiHealth, AuthUser } from "@/lib/types";

const MIN_PASSWORD_LENGTH = 12; // mirrors RegisterRequest on the API

interface Props {
  health: ApiHealth | null;
  notice?: string | null;
  onAuthenticated: (user: AuthUser) => void;
}

export default function AuthPanel({ health, notice, onAuthenticated }: Props) {
  const [kind, setKind] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const unavailable = health !== null && !health.authConfigured;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (kind === "register" && password.length < MIN_PASSWORD_LENGTH) {
      setError(`Use at least ${MIN_PASSWORD_LENGTH} characters for the password`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onAuthenticated(await authenticate(kind, email.trim(), password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm">
      <header className="mb-6">
        <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">Oxygen AI</p>
        <h1 className="mt-1 text-2xl font-semibold">
          {kind === "login" ? "Sign in" : "Create an account"}
        </h1>
        <p className="mt-2 text-sm text-[#7C8591]">
          Analyses are attributed to your account and rate-limited per user.
        </p>
      </header>

      {notice && (
        <p className="mb-4 rounded-md border border-[#3a3320] bg-[#1c1a12] px-3 py-2 text-sm text-[#E8C26A]">
          {notice}
        </p>
      )}
      {unavailable && (
        <p className="mb-4 rounded-md border border-[#3a2022] bg-[#1c1214] px-3 py-2 text-sm text-[#F28B82]">
          This server has no JWT_SECRET configured, so sign-in is unavailable. Set it in
          apps/api/.env.local (see apps/api/.env.example) and restart the API.
        </p>
      )}

      <form onSubmit={submit} className="flex flex-col gap-3">
        <input
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="Email"
          className="rounded-md border border-[#232830] bg-[#12151B] px-3 py-2 text-sm outline-none focus:border-[#4FD1C5] focus:ring-1 focus:ring-[#4FD1C5]"
        />
        <input
          type="password"
          required
          autoComplete={kind === "login" ? "current-password" : "new-password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={kind === "register" ? `Password (min ${MIN_PASSWORD_LENGTH} characters)` : "Password"}
          className="rounded-md border border-[#232830] bg-[#12151B] px-3 py-2 text-sm outline-none focus:border-[#4FD1C5] focus:ring-1 focus:ring-[#4FD1C5]"
        />
        <button
          type="submit"
          disabled={busy || unavailable}
          className="rounded-md bg-[#4FD1C5] px-4 py-2 text-sm font-medium text-[#0A0C10] transition disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Please wait…" : kind === "login" ? "Sign in" : "Register"}
        </button>
      </form>

      {error && <p className="mt-3 text-sm text-[#F28B82]">{error}</p>}

      <button
        type="button"
        onClick={() => {
          setKind(kind === "login" ? "register" : "login");
          setError(null);
        }}
        className="mt-4 text-sm text-[#7C8591] underline-offset-2 hover:text-[#E8EAED] hover:underline"
      >
        {kind === "login" ? "No account yet? Register" : "Already registered? Sign in"}
      </button>
    </div>
  );
}
