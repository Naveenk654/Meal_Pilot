-- §10 weekly weight update — audit trail of every weight the user records.
-- Recompute-target rule (WEIGHT_RECOMPUTE_THRESHOLD_KG in nutrition.py) is
-- decided at application layer; this table just stores history.

create table if not exists public.weight_logs (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  weight_kg numeric not null check (weight_kg > 0),
  logged_at timestamptz not null default now(),
  note text null
);

create index if not exists idx_weight_logs_user_time on public.weight_logs(user_id, logged_at desc);

alter table public.weight_logs enable row level security;
drop policy if exists weight_logs_self_all on public.weight_logs;
create policy weight_logs_self_all on public.weight_logs
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());
