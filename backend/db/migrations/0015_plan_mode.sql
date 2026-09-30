-- 0015 — add plan_mode to user_profile so users can opt into a
-- mess+canteen mixed plan, or keep mess-only (default).
--
-- mess_only: current behavior. Canteen fallback fires when mess is
--            infeasible OR when the winning candidate's soft score is
--            below settings.fallback_quality_threshold (protects against
--            "4 servings of tea to hit target"-style plans).
-- mixed:     augment every mess candidate with canteen items up to
--            user_profile.budget_soft_inr, regardless of feasibility.

do $$ begin
  create type plan_mode as enum ('mess_only', 'mixed');
exception when duplicate_object then null; end $$;

alter table public.user_profile
  add column if not exists plan_mode plan_mode not null default 'mess_only';

comment on column public.user_profile.plan_mode is
  'Planner source policy. mess_only = mess first, canteen fallback on infeasible/low-quality. mixed = always augment with canteen up to budget_soft_inr.';
