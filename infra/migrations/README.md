# Database

One migration: `0001_init.sql` — every table from the blueprint's full
schema (Passage 1 §9.1's corrected `ai_comparisons` + the ~34 tables
Passage 4 §5.2 restored), with the constraints, FK `ON DELETE` policies,
and indexes Passage 4 §5.3 specifies.

## Verified before delivery (actually run, not claimed)

```
$ apt-get install postgresql postgresql-contrib postgresql-16-pgvector
$ service postgresql start
$ createdb oxygen_ai_test
$ psql -v ON_ERROR_STOP=1 -f infra/migrations/0001_init.sql oxygen_ai_test
    → every CREATE TABLE / CREATE INDEX succeeded (38 tables)
$ psql -c "insert a row with status='not-a-real-status'" oxygen_ai_test
    → correctly REJECTED: "violates check constraint users_status_check"
$ psql -c "insert a valid ai_providers row" oxygen_ai_test
    → succeeded
```

Full output is in the chat message that shipped this file. The test
database was dropped after verification — nothing persists from this
sandbox.

## Applying it yourself

```bash
createdb oxygen_ai
psql -f infra/migrations/0001_init.sql oxygen_ai
```

Requires the `pgvector` extension available on the server (`CREATE
EXTENSION vector` is in the migration). Most managed Postgres providers
(Supabase, Neon, RDS with the right parameter group) ship it; a local
install needs the `postgresql-<version>-pgvector` package.

## What this is NOT yet

- **Not wired to the app.** `app/api/analyze/route.ts` still runs on
  synthetic in-memory OHLCV and never touches this schema. Connecting
  them means: a `DATABASE_URL`, a query layer (raw `pg`, or an ORM —
  neither is installed yet, deliberately, since picking one is a real
  decision), and rewriting the route to read/write `market_instruments`,
  `trading_signals`, etc. instead of generating data per-request. That's
  the natural next increment after this file, not part of it.
- **No migration tool.** This is one hand-written SQL file, not a
  Prisma/Drizzle/node-pg-migrate setup with up/down migrations and a
  tracked migration history. Fine for an initial schema; matters once you
  need to evolve it.
- **Retention policy is documented, not enforced.** The blueprint calls
  for rolling old tick-level `market_data` into cold storage — that's a
  scheduled job against a real deployment target, which doesn't exist
  here.