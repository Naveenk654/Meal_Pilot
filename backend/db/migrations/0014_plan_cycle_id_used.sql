-- 0014 — track which menu_cycle a plan was built from so `menu_stale`
-- detection is deterministic instead of inferred by dish-name lookup
-- (which broke whenever the same dish appeared in multiple cycles).

alter table public.daily_plans
  add column if not exists cycle_id_used bigint
  references public.menu_cycles(id);

create index if not exists idx_daily_plans_cycle_id_used
  on public.daily_plans(cycle_id_used);
