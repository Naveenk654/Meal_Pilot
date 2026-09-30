-- §17, §20 — Row-Level Security. Every user-scoped table filters by user_id = auth.uid().
-- Menu / macro / canteen tables are read-only for students, write for admins.

-- Helper: is caller an admin? Uses public.users.role.
create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce((select role = 'admin' from public.users where id = auth.uid()), false);
$$;

-- --- users -------------------------------------------------------------------
alter table public.users enable row level security;

drop policy if exists users_self_select on public.users;
create policy users_self_select on public.users
  for select using (id = auth.uid() or public.is_admin());

drop policy if exists users_self_upsert on public.users;
create policy users_self_upsert on public.users
  for insert with check (id = auth.uid());

drop policy if exists users_self_update on public.users;
create policy users_self_update on public.users
  for update using (id = auth.uid()) with check (id = auth.uid());

-- --- user_profile -----------------------------------------------------------
alter table public.user_profile enable row level security;
drop policy if exists user_profile_self_all on public.user_profile;
create policy user_profile_self_all on public.user_profile
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());

-- --- user_preferences -------------------------------------------------------
alter table public.user_preferences enable row level security;
drop policy if exists user_preferences_self_all on public.user_preferences;
create policy user_preferences_self_all on public.user_preferences
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());

-- --- user_memory_behavioral -------------------------------------------------
alter table public.user_memory_behavioral enable row level security;
drop policy if exists user_memory_self_all on public.user_memory_behavioral;
create policy user_memory_self_all on public.user_memory_behavioral
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());

-- --- menu / macro / canteen (read for students, write for admins) -----------
alter table public.macro_db enable row level security;
drop policy if exists macro_db_read_all on public.macro_db;
create policy macro_db_read_all on public.macro_db for select using (auth.uid() is not null);
drop policy if exists macro_db_admin_write on public.macro_db;
create policy macro_db_admin_write on public.macro_db for all
  using (public.is_admin()) with check (public.is_admin());

alter table public.menu_cycles enable row level security;
drop policy if exists menu_cycles_read_all on public.menu_cycles;
create policy menu_cycles_read_all on public.menu_cycles for select using (auth.uid() is not null);
drop policy if exists menu_cycles_admin_write on public.menu_cycles;
create policy menu_cycles_admin_write on public.menu_cycles for all
  using (public.is_admin()) with check (public.is_admin());

alter table public.menu_items enable row level security;
drop policy if exists menu_items_read_all on public.menu_items;
create policy menu_items_read_all on public.menu_items for select using (auth.uid() is not null);
drop policy if exists menu_items_admin_write on public.menu_items;
create policy menu_items_admin_write on public.menu_items for all
  using (public.is_admin()) with check (public.is_admin());

alter table public.canteen_items enable row level security;
drop policy if exists canteen_items_read_all on public.canteen_items;
create policy canteen_items_read_all on public.canteen_items for select using (auth.uid() is not null);
drop policy if exists canteen_items_admin_write on public.canteen_items;
create policy canteen_items_admin_write on public.canteen_items for all
  using (public.is_admin()) with check (public.is_admin());

-- --- daily_plans ------------------------------------------------------------
alter table public.daily_plans enable row level security;
drop policy if exists daily_plans_self_all on public.daily_plans;
create policy daily_plans_self_all on public.daily_plans
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());

-- --- meal_logs (append-only, read + insert for self) ------------------------
alter table public.meal_logs enable row level security;
drop policy if exists meal_logs_self_select on public.meal_logs;
create policy meal_logs_self_select on public.meal_logs
  for select using (user_id = auth.uid() or public.is_admin());
drop policy if exists meal_logs_self_insert on public.meal_logs;
create policy meal_logs_self_insert on public.meal_logs
  for insert with check (user_id = auth.uid());
-- No update / delete policies: append-only + trigger enforces it.

-- --- hitl_requests ----------------------------------------------------------
alter table public.hitl_requests enable row level security;
drop policy if exists hitl_requests_self_all on public.hitl_requests;
create policy hitl_requests_self_all on public.hitl_requests
  for all using (user_id = auth.uid() or public.is_admin())
  with check (user_id = auth.uid());

-- --- observability (admin-read; service-role writes bypass RLS anyway) -------
alter table public.agent_runs enable row level security;
drop policy if exists agent_runs_self_or_admin_read on public.agent_runs;
create policy agent_runs_self_or_admin_read on public.agent_runs
  for select using (user_id = auth.uid() or public.is_admin());

alter table public.tool_calls enable row level security;
drop policy if exists tool_calls_admin_read on public.tool_calls;
create policy tool_calls_admin_read on public.tool_calls
  for select using (public.is_admin());

alter table public.decision_traces enable row level security;
drop policy if exists decision_traces_admin_read on public.decision_traces;
create policy decision_traces_admin_read on public.decision_traces
  for select using (public.is_admin());
