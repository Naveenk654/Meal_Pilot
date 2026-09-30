-- §17 hitl_requests + §14 I6 response idempotency.

create table if not exists public.hitl_requests (
  id bigserial primary key,
  user_id uuid not null references public.users(id) on delete cascade,
  agent text not null,
  question text not null,
  options_json jsonb not null default '[]'::jsonb,
  context_json jsonb not null default '{}'::jsonb,
  status hitl_status not null default 'pending',
  response_json jsonb null,
  response_hash text null,
  responded_at timestamptz null,
  created_at timestamptz not null default now(),
  -- I6: duplicate approvals deduped
  constraint uq_hitl_response_idempotent unique (id, response_hash)
);

create index if not exists idx_hitl_user_status on public.hitl_requests(user_id, status);
