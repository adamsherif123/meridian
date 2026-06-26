-- Walking-skeleton ping table.
-- Paste this into: Supabase dashboard → SQL Editor → New query → Run

create table if not exists skeleton_pings (
  id         uuid        primary key default gen_random_uuid(),
  created_at timestamptz not null    default now(),
  source     text,
  note       text
);
