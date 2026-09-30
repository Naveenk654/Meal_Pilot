-- §13, §14, §17 — plan lifecycle + append-only meal logs.

create table if not exists public.daily_plans (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  date date not null,
  plan_json jsonb not null,
  target_macros_json jsonb not null,
  validation_result_json jsonb not null,
  confidence numeric not null check (confidence between 0 and 1),
  confidence_factors jsonb not null,
  degraded_mode boolean not null default false,
  status plan_status not null default 'draft',
  supersedes_id bigint null references public.daily_plans(id),
  created_at timestamptz not null default now(),
  sent_at timestamptz null,
  completed_at timestamptz null,
  superseded_at timestamptz null,
  version int not null default 1  -- TX2: if_version guard for concurrent replans
);

create index if not exists idx_daily_plans_user_date on public.daily_plans(user_id, date);
create index if not exists idx_daily_plans_user_date_status on public.daily_plans(user_id, date, status);

create table if not exists public.meal_logs (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  date date not null,
  meal meal_slot not null,
  event_id uuid not null unique,  -- I2: client-generated idempotency key
  action meal_action not null,
  actual_dishes_json jsonb not null default '[]'::jsonb,
  actual_macros_json jsonb not null default '{}'::jsonb,
  note text null,
  logged_at timestamptz not null default now()
);
-- Append-only: no updates allowed (enforced via trigger below).

create index if not exists idx_meal_logs_user_date on public.meal_logs(user_id, date);

create or replace function public.forbid_meal_log_update()
returns trigger as $$
begin
  raise exception 'meal_logs is append-only (§13, A10)';
end;
$$ language plpgsql;

drop trigger if exists trg_meal_logs_no_update on public.meal_logs;
create trigger trg_meal_logs_no_update
before update on public.meal_logs
for each row execute function public.forbid_meal_log_update();
