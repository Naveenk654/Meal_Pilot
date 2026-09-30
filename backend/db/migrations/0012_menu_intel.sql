-- M2: menu_intelligence extensions.
--
-- 1. menu_cycles.status — a freshly-ingested cycle lands as 'draft' and only
--    goes 'active' after admin approval. Prior active cycle for the same
--    effective window transitions to 'superseded' at approval time.
-- 2. menu_cycles.pending_review_json — LLM extractions with per-dish
--    confidence < CONFIDENCE_THRESHOLD are held here and surfaced to the
--    admin verification UI; approved items move into menu_items.
-- 3. menu_cycles.ingested_by — nullable FK to public.users so the audit trail
--    identifies the admin who uploaded the PDF.
-- 4. menu_events — append-only ledger of `menu_updated` events (§6.1 step 7)
--    that the Planner will consume when M3 wires up menu-driven replans.

do $$ begin
  create type menu_cycle_status as enum ('draft', 'active', 'superseded');
exception when duplicate_object then null; end $$;

alter table public.menu_cycles
  add column if not exists status menu_cycle_status not null default 'draft';

alter table public.menu_cycles
  add column if not exists pending_review_json jsonb not null default '[]'::jsonb;

alter table public.menu_cycles
  add column if not exists ingested_by uuid null references public.users(id) on delete set null;

alter table public.menu_cycles
  add column if not exists approved_at timestamptz null;

alter table public.menu_cycles
  add column if not exists approved_by uuid null references public.users(id) on delete set null;

-- Only one active cycle per (source, effective_from) pair at a time. Combined
-- with the existing UNIQUE(content_hash, effective_from, source) this makes
-- both "same PDF re-ingested" and "two live cycles for the same window"
-- structurally impossible.
create unique index if not exists uq_menu_cycles_one_active
  on public.menu_cycles (source, effective_from)
  where status = 'active';

create index if not exists idx_menu_cycles_status on public.menu_cycles(status, effective_from);

-- §6.1 step 7 — every successful ingestion emits `menu_updated`. Append-only.
create table if not exists public.menu_events (
  id bigserial primary key,
  event_type text not null,               -- 'menu_updated' for now, room for more
  cycle_id bigint not null references public.menu_cycles(id) on delete cascade,
  effective_from date not null,
  effective_to date not null,
  emitted_at timestamptz not null default now(),
  payload jsonb not null default '{}'::jsonb
);

create index if not exists idx_menu_events_emitted on public.menu_events(emitted_at desc);
create index if not exists idx_menu_events_cycle on public.menu_events(cycle_id);

alter table public.menu_events enable row level security;
drop policy if exists menu_events_read_all on public.menu_events;
create policy menu_events_read_all on public.menu_events
  for select using (auth.uid() is not null);
drop policy if exists menu_events_admin_write on public.menu_events;
create policy menu_events_admin_write on public.menu_events
  for all using (public.is_admin()) with check (public.is_admin());
