# Migrations

Numbered SQL files. Apply in order against the Supabase Postgres instance.

Two ways to apply:

**Supabase SQL editor** — paste each file in order.

**psql** — with the direct connection string (Supabase → Project Settings → Database → Connection string):
```
psql "$SUPABASE_DB_URL" -f 0001_extensions.sql
psql "$SUPABASE_DB_URL" -f 0002_enums.sql
...
```

Idempotent: each migration uses `if not exists` / `do $$ ... exception when duplicate_object`. Re-running is safe.
