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

-- ── Session 4.1: board subject + scope containers ────────────────────────

-- Board-level subject: what entity the process is keyed on.
-- Run once; safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
alter table boards add column if not exists meta jsonb not null default '{}';

-- ── Session 4: sample file uploads ───────────────────────────────────────

-- ── Session 5a: AI-check gate ────────────────────────────────────────────

-- gate_comments: one row per gap question produced by the AI-check gate
-- No RLS — accessed via service key only (consistent with boards/sample_files).
create table if not exists gate_comments (
  id         uuid        primary key default gen_random_uuid(),
  board_id   uuid        references boards(id) on delete cascade,
  node_id    text,                        -- anchored node id (null = board-level)
  severity   text        not null,        -- 'blocking' | 'advisory'
  status     text        not null default 'open',  -- 'open' | 'answered' | 'resolved' | 'rejected'
  question   text        not null,
  answer     text,                        -- user's answer (null until answered)
  round      int         not null default 1,        -- which review round produced it
  created_at timestamptz not null default now()
);

-- ── Session 5b: verify loop + freeze gate ────────────────────────────────

-- Extend gate_comments with fields needed for verify + follow-up
alter table gate_comments add column if not exists followup    text;
alter table gate_comments add column if not exists parent_id   uuid references gate_comments(id);
alter table gate_comments add column if not exists resolved_at timestamptz;

-- frozen_specs: single replaceable spec snapshot per board (re-freeze = upsert)
create table if not exists frozen_specs (
  board_id  uuid        primary key references boards(id) on delete cascade,
  spec      jsonb       not null,
  frozen_at timestamptz not null default now()
);

-- ── Session 7: email dedup guard (Temporal agent runtime) ───────────────────

-- processed_emails: prevents an agent from processing the same Gmail message twice.
-- Keyed by Gmail RFC-822 message-id. board_id scopes it per-agent (null = global).
create table if not exists processed_emails (
  id           uuid        primary key default gen_random_uuid(),
  message_id   text        not null,
  board_id     uuid        references boards(id) on delete cascade,
  processed_at timestamptz not null default now(),
  agent_run_id text,                          -- optional: Temporal workflow run-id
  unique (message_id, board_id)               -- prevents double-insert per (msg, agent)
);

create index if not exists processed_emails_message_id_idx
  on processed_emails (message_id);

-- ── Session 8: codegen records ────────────────────────────────────────────────

-- generated_agents: one row per board; upserted on every codegen run.
-- status: 'valid' = passed all checks, 'invalid' = failed after all repair attempts.
-- errors: jsonb array of validation error strings (null when valid).
create table if not exists generated_agents (
  id          uuid        primary key default gen_random_uuid(),
  board_id    uuid        not null references boards(id) on delete cascade,
  file_path   text        not null,
  status      text        not null,             -- 'valid' | 'invalid'
  attempts    int         not null default 1,   -- how many LLM attempts were needed
  errors      jsonb,                            -- null when valid
  created_at  timestamptz not null default now(),
  unique (board_id)                             -- one current record per board; upserted
);

create index if not exists generated_agents_board_id_idx
  on generated_agents (board_id);

-- ── Session 9: eval harness results ──────────────────────────────────────────

-- evals: latest eval result per board (upserted on each run).
-- field_results: jsonb array of {field, expected, actual, passed, note} objects.
-- passed=null on a field means expected was a placeholder (not yet filled in).
create table if not exists evals (
  id            uuid        primary key default gen_random_uuid(),
  board_id      uuid        not null references boards(id) on delete cascade,
  case_name     text        not null,
  passed        bool        not null,
  field_results jsonb       not null,   -- answer-key per-field results
  consistency   jsonb,                  -- consistency check results (S9.1)
  raw_csv       text,
  created_at    timestamptz not null default now(),
  unique (board_id)                     -- one current record per board; upserted
);

-- S9.1 migration: add consistency column if table already exists from S9
alter table evals add column if not exists consistency jsonb;

create index if not exists evals_board_id_idx
  on evals (board_id);
create index if not exists evals_created_at_idx
  on evals (board_id, created_at desc);

-- ── Session 10: self-heal loop records ───────────────────────────────────────

-- heal_runs: one row per heal loop invocation (INSERT, not upsert — history is kept).
-- status:  'healed'              — all consistency error-checks passed
--          'max_attempts'        — MAX_ATTEMPTS (5) reached without passing
--          'stalled'             — STALL_LIMIT consecutive attempts with no improvement
--          'revert_validation'   — coding agent produced a broken file; reverted
--          'revert_regression'   — patch made things worse; reverted to best-known-good
--          'agent_error'         — coding agent raised an exception
-- history: jsonb array of per-attempt records {attempt, eval_passed, pass_count,
--          total_checks, failed_checks, delta, agent_turns?, regression?,
--          validation_errors?}
create table if not exists heal_runs (
  id          uuid        primary key default gen_random_uuid(),
  board_id    uuid        not null references boards(id) on delete cascade,
  status      text        not null,
  attempts    int         not null default 0,
  history     jsonb       not null default '[]',
  final_eval  jsonb,
  created_at  timestamptz not null default now()
);

create index if not exists heal_runs_board_id_idx
  on heal_runs (board_id);
create index if not exists heal_runs_created_at_idx
  on heal_runs (board_id, created_at desc);

-- ── Session 11a: live agent run results ──────────────────────────────────────

-- agent_runs: one row per live Gmail → agent run (INSERT, not upsert — full history kept).
-- status: 'completed' | 'failed'
-- result_json: the 8 CSV report columns as a flat jsonb object
--   {shipment_number, invoices_processed, invoices_succeeded, invoices_failed,
--    goods_failed, batches_processed, batches_succeeded, batches_failed}
-- csv_content: the raw CSV string from emit_report
create table if not exists agent_runs (
  id            uuid        primary key default gen_random_uuid(),
  board_id      uuid        not null references boards(id) on delete cascade,
  message_id    text        not null,   -- Gmail hex ID (dedup key)
  subject       text,                   -- shipment_number / email subject
  status        text        not null default 'completed',
  csv_content   text,
  result_json   jsonb,
  created_at    timestamptz not null default now()
);

create index if not exists agent_runs_board_id_idx
  on agent_runs (board_id);
create index if not exists agent_runs_created_at_idx
  on agent_runs (board_id, created_at desc);

-- sample_files: metadata + extracted text for per-node file attachments
-- Storage bucket "sample-files" must be created once in the Supabase dashboard.
create table if not exists sample_files (
  id             uuid        primary key default gen_random_uuid(),
  board_id       uuid,
  node_id        text,
  filename       text,
  mime           text,
  storage_path   text,
  extracted_text text,
  created_at     timestamptz not null default now()
);
