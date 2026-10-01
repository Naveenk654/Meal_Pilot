-- 0017 — lock public.users.role against self-promotion.
--
-- 0009_rls_policies.sql's users_self_update policy allows a user to update
-- their own row; 0010_grants.sql grants them UPDATE on public.users. Without
-- a column-level guard, any logged-in user can run
--    update public.users set role = 'admin' where id = auth.uid();
-- and gain the admin bypass that is_admin() grants via RLS.
--
-- This trigger rejects any INSERT or UPDATE that writes a non-null role when
-- the caller is NOT the service_role. Admin promotion stays possible via the
-- service-role client (used by migrations and future admin ops) but is
-- blocked for anon / authenticated JWTs.

create or replace function public.enforce_user_role_locked()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.role() = 'service_role' then
    return new;
  end if;

  if tg_op = 'INSERT' then
    -- New rows from a non-service caller may only land with the default role
    -- ('student'). Any attempt to set role='admin' etc. is rejected.
    if new.role is distinct from 'student' then
      raise exception
        'cannot set users.role on insert from non-service_role (got %)',
        new.role
        using errcode = '42501';
    end if;
    return new;
  end if;

  -- UPDATE: role must not change from old to new unless caller is service_role.
  if new.role is distinct from old.role then
    raise exception
      'cannot change users.role from % to % without service_role',
      old.role, new.role
      using errcode = '42501';
  end if;

  return new;
end;
$$;

drop trigger if exists users_role_lock_ins on public.users;
create trigger users_role_lock_ins
  before insert on public.users
  for each row execute function public.enforce_user_role_locked();

drop trigger if exists users_role_lock_upd on public.users;
create trigger users_role_lock_upd
  before update of role on public.users
  for each row execute function public.enforce_user_role_locked();
