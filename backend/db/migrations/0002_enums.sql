-- Enum types. Kept in one file so changes are auditable.

do $$ begin
  create type user_mode as enum ('fitness', 'general');
exception when duplicate_object then null; end $$;

do $$ begin
  create type user_role as enum ('student', 'admin');
exception when duplicate_object then null; end $$;

do $$ begin
  create type activity_level as enum ('sedentary', 'light', 'gym_3_4', 'gym_5_6', 'athlete');
exception when duplicate_object then null; end $$;

do $$ begin
  create type gender as enum ('male', 'female', 'other');
exception when duplicate_object then null; end $$;

do $$ begin
  create type goal as enum ('cut', 'maintain', 'bulk');
exception when duplicate_object then null; end $$;

do $$ begin
  create type preference_kind as enum ('allergy', 'dislike', 'like', 'supplement', 'restriction');
exception when duplicate_object then null; end $$;

do $$ begin
  create type preference_source as enum ('onboarding', 'learning', 'manual');
exception when duplicate_object then null; end $$;

do $$ begin
  create type preference_authority as enum ('hard', 'soft');
exception when duplicate_object then null; end $$;

do $$ begin
  create type preference_status as enum ('active', 'retired');
exception when duplicate_object then null; end $$;

do $$ begin
  create type memory_status as enum ('proposed', 'active', 'retired');
exception when duplicate_object then null; end $$;

do $$ begin
  create type meal_slot as enum ('breakfast', 'lunch', 'snack', 'dinner');
exception when duplicate_object then null; end $$;

do $$ begin
  create type meal_action as enum ('ate_planned', 'ate_different', 'skipped');
exception when duplicate_object then null; end $$;

do $$ begin
  create type macro_source as enum ('manual', 'ifct', 'nutritionix', 'llm_estimated');
exception when duplicate_object then null; end $$;

do $$ begin
  create type plan_status as enum ('draft', 'validated', 'sent', 'completed', 'superseded');
exception when duplicate_object then null; end $$;

do $$ begin
  create type hitl_status as enum ('pending', 'approved', 'rejected', 'expired', 'edited');
exception when duplicate_object then null; end $$;

do $$ begin
  create type tool_call_status as enum ('ok', 'error', 'timeout');
exception when duplicate_object then null; end $$;

do $$ begin
  create type menu_source as enum ('pdf', 'email', 'manual');
exception when duplicate_object then null; end $$;
