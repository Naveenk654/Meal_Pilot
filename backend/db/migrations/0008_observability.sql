-- §15 observability tables. Structured decision data is the authoritative audit trail.

create table if not exists public.agent_runs (
  id bigserial primary key,
  idempotency_key text not null unique,   -- I1, I3, I5
  user_id uuid null references public.users(id) on delete set null,
  agent text not null,
  trigger text not null,
  triggering_event_id text null,
  planner_state_snapshot jsonb null,
  final_outcome jsonb null,
  confidence numeric null check (confidence is null or confidence between 0 and 1),
  confidence_factors jsonb null,
  hitl_status hitl_status null,
  degraded_mode boolean not null default false,
  started_at timestamptz not null default now(),
  ended_at timestamptz null,
  latency_ms int null,
  total_tokens int null,
  total_cost_inr numeric null
);

create index if not exists idx_agent_runs_user_agent on public.agent_runs(user_id, agent, started_at desc);

create table if not exists public.tool_calls (
  id bigserial primary key,
  agent_run_id bigint not null references public.agent_runs(id) on delete cascade,
  tool_name text not null,
  input_snapshot jsonb null,
  output_snapshot jsonb null,
  status tool_call_status not null,
  latency_ms int null,
  tokens_used int null,
  cost_inr numeric null,
  called_at timestamptz not null default now()
);

create index if not exists idx_tool_calls_run on public.tool_calls(agent_run_id, called_at);

create table if not exists public.decision_traces (
  id bigserial primary key,
  agent_run_id bigint not null references public.agent_runs(id) on delete cascade,
  step_name text not null,
  candidates_considered jsonb null,
  validation_results jsonb null,
  chosen_option jsonb null,
  reason_code text not null,   -- O3: structured, authoritative
  llm_reasoning text null,     -- O4: secondary, optional
  created_at timestamptz not null default now()
);

create index if not exists idx_decision_traces_run on public.decision_traces(agent_run_id, created_at);
