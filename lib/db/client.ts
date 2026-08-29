import { Pool } from "pg";

let pool: Pool | null = null;

export function isDbConfigured(): boolean {
  return Boolean(process.env.DATABASE_URL);
}

/**
 * Throws if DATABASE_URL isn't set — callers must check isDbConfigured()
 * first (the API route does, and falls back to ephemeral synthetic data
 * when it's false, rather than calling this).
 */
export function getPool(): Pool {
  if (!process.env.DATABASE_URL) {
    throw new Error("DATABASE_URL is not set");
  }
  if (!pool) {
    pool = new Pool({ connectionString: process.env.DATABASE_URL });
  }
  return pool;
}