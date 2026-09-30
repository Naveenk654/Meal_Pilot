-- 0016 — choice_group_id on menu_items so the extractor can flag
-- "pick one of these" alternatives. Example: a menu cell listing
-- "Tea / Coffee / Milk" becomes three menu_items rows sharing the same
-- choice_group_id in the same (cycle, day, meal). The Planner treats
-- them as mutually exclusive — pick exactly one, never all three.
--
-- Deterministic ids (md5 of source_string + day + meal) mean re-uploading
-- the same PDF produces the same group ids, keeping the diff logic stable.

alter table public.menu_items
  add column if not exists choice_group_id text null;

create index if not exists idx_menu_items_choice_group
  on public.menu_items(cycle_id, day_of_week, meal, choice_group_id)
  where choice_group_id is not null;

comment on column public.menu_items.choice_group_id is
  'When set, this menu_item is one of several alternatives in the same slot. '
  'Planner picks EXACTLY ONE dish per (cycle_id, day, meal, choice_group_id) '
  'tuple. NULL means the dish is not part of a choice group (standalone).';
