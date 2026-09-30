-- §11, §12, §17 — menu cycles, items, canonical macro DB, canteen items.

create table if not exists public.macro_db (
  id bigserial primary key,
  dish_name_normalized text not null unique,
  serving_unit text not null,
  serving_grams numeric not null check (serving_grams > 0),
  kcal numeric not null check (kcal >= 0),
  protein_g numeric not null check (protein_g >= 0),
  carbs_g numeric not null check (carbs_g >= 0),
  fats_g numeric not null check (fats_g >= 0),
  is_veg boolean not null,
  practical_max_servings_per_day numeric not null check (practical_max_servings_per_day > 0),
  source macro_source not null,
  confidence numeric not null default 1.0 check (confidence between 0 and 1),
  verified boolean not null default false,
  estimated_at timestamptz null,
  verified_at timestamptz null,
  verified_by uuid null references public.users(id)
);

create table if not exists public.menu_cycles (
  id bigserial primary key,
  effective_from date not null,
  effective_to date not null check (effective_to >= effective_from),
  source menu_source not null,
  version int not null default 1,
  content_hash text not null,
  ingested_at timestamptz not null default now(),
  pdf_url text null,
  -- I4: same PDF cannot become multiple cycles
  constraint uq_menu_cycles_idempotent unique (content_hash, effective_from, source)
);

create index if not exists idx_menu_cycles_effective on public.menu_cycles(effective_from, effective_to);

create table if not exists public.menu_items (
  id bigserial primary key,
  cycle_id bigint not null references public.menu_cycles(id) on delete cascade,
  day_of_week int not null check (day_of_week between 0 and 6),
  meal meal_slot not null,
  dish_name text not null,
  is_veg boolean not null,
  macro_id bigint null references public.macro_db(id),
  confidence numeric not null default 1.0 check (confidence between 0 and 1)
);

create index if not exists idx_menu_items_cycle_day on public.menu_items(cycle_id, day_of_week);

create table if not exists public.canteen_items (
  id bigserial primary key,
  shop_name text not null,
  dish text not null,
  price_inr numeric not null check (price_inr >= 0),
  is_veg boolean not null,
  macro_id bigint null references public.macro_db(id),
  available_hours text not null,
  practical_max_servings_per_day numeric not null check (practical_max_servings_per_day > 0)
);

create index if not exists idx_canteen_shop_dish on public.canteen_items(shop_name, dish);
