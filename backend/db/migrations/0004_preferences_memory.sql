-- §17 preferences (three-kind model) and behavioral memory.

create table if not exists public.user_preferences (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  kind preference_kind not null,
  value text not null,
  source preference_source not null,
  authority preference_authority not null,
  confidence numeric not null default 1.0 check (confidence between 0 and 1),
  status preference_status not null default 'active',
  superseded_by bigint null references public.user_preferences(id),
  first_seen timestamptz not null default now(),
  last_confirmed_at timestamptz not null default now(),
  retired_at timestamptz null
);

create index if not exists idx_user_preferences_user on public.user_preferences(user_id, status);

create table if not exists public.user_memory_behavioral (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  fact text not null,
  evidence jsonb not null default '{}'::jsonb,
  status memory_status not null default 'proposed',
  first_seen timestamptz not null default now(),
  last_confirmed_at timestamptz not null default now(),
  retired_at timestamptz null,
  contradiction_count int not null default 0
);

create index if not exists idx_user_memory_user on public.user_memory_behavioral(user_id, status);
