-- Walking-skeleton ping table.
-- Paste this into: Supabase dashboard → SQL Editor → New query → Run

create table if not exists skeleton_pings (
  id         uuid        primary key default gen_random_uuid(),
  created_at timestamptz not null    default now(),
  source     text,
  note       text
);

-- ── Session 3: canvas persistence ────────────────────────────────────────

-- boards: one row per canvas board
create table if not exists boards (
  id         uuid        primary key default gen_random_uuid(),
  name       text        not null,
  created_at timestamptz not null    default now(),
  updated_at timestamptz not null    default now()
);

-- board_graphs: stores the full node/edge graph as jsonb blobs keyed by board
create table if not exists board_graphs (
  board_id   uuid        primary key references boards(id) on delete cascade,
  nodes      jsonb       not null    default '[]',
  edges      jsonb       not null    default '[]',
  updated_at timestamptz not null    default now()
);
