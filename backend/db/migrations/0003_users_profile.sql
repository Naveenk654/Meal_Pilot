-- §17. public.users mirrors auth.users id; profile is 1:1 with user.

create table if not exists public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  created_at timestamptz not null default now(),
  timezone text not null default 'Asia/Kolkata',
  mode user_mode not null default 'general',
  role user_role not null default 'student'
);

create table if not exists public.user_profile (
  user_id uuid primary key references public.users(id) on delete cascade,
  age int not null check (age between 10 and 100),
  gender gender not null,
  height_cm numeric not null check (height_cm between 100 and 250),
  weight_kg numeric not null check (weight_kg between 30 and 250),
  activity_level activity_level not null,
  goal goal not null,
  veg_default boolean not null default true,
  budget_soft_inr numeric not null check (budget_soft_inr >= 0),
  budget_hard_inr numeric null,
  bmr numeric not null,
  tdee numeric not null,
  target_kcal numeric not null,
  target_protein_g numeric not null,
  target_carbs_g numeric not null,
  target_fats_g numeric not null,
  updated_at timestamptz not null default now()
);
