-- Supabase built-in roles need table-level GRANTs on public.*.
-- RLS (0009) then filters WHICH rows each role can see; GRANT decides whether
-- the role can touch the table at all. Without these, every write fails with
-- SQLSTATE 42501 "permission denied", even for service_role.

grant usage on schema public to anon, authenticated, service_role;

-- Full access for service_role (backend/service-side writes; bypasses RLS anyway).
grant all privileges on all tables    in schema public to service_role;
grant all privileges on all sequences in schema public to service_role;
grant all privileges on all functions in schema public to service_role;

-- CRUD for the two user-facing roles. RLS policies from 0009 still restrict rows.
grant select, insert, update, delete on all tables    in schema public to authenticated;
grant usage,  select                on all sequences in schema public to authenticated;
grant execute                        on all functions in schema public to authenticated;

grant select on all tables    in schema public to anon;
grant usage, select on all sequences in schema public to anon;

-- Ensure future tables created in this schema inherit the same privileges,
-- so we don't have to grant again after every migration.
alter default privileges in schema public
  grant all privileges on tables to service_role;
alter default privileges in schema public
  grant all privileges on sequences to service_role;
alter default privileges in schema public
  grant execute on functions to service_role;

alter default privileges in schema public
  grant select, insert, update, delete on tables to authenticated;
alter default privileges in schema public
  grant usage, select on sequences to authenticated;
alter default privileges in schema public
  grant execute on functions to authenticated;

alter default privileges in schema public
  grant select on tables to anon;
alter default privileges in schema public
  grant usage, select on sequences to anon;
