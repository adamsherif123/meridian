export type NodeKind =
  | 'trigger'
  | 'expected_document'
  | 'extract_validate'
  | 'match_documents'
  | 'decision'
  | 'aggregate'    // legacy alias — old boards keep loading; displays as "Count"
  | 'count'
  | 'summarize'
  | 'tool_action'
  | 'report'
  | 'custom'
  | 'scope'
  | 'assumption'

export type EdgeKind = 'default' | 'on_pass' | 'on_fail' | 'exception' | 'custom'

// ── Block types ───────────────────────────────────────────────────────────

export type BlockKind =
  | 'required_field'
  | 'match_key'
  | 'count_key'
  | 'branch_condition'
  | 'sample_file'
  | 'note'
  | 'doc_field'

export interface RequiredFieldBlock {
  id: string; kind: 'required_field'; name: string; scope: 'line_item' | 'document'; required: boolean
}

export interface MatchKeyBlock {
  id: string; kind: 'match_key'
  source_collection: string
  target_collection: string
  key_field: string
  on_missing: 'fail' | 'flag' | 'ignore'
}

export interface CountKeyBlock {
  id: string; kind: 'count_key'
  collection: string
  dedup_key: string
  label: string
  track: ('processed' | 'succeeded' | 'failed')[]
}

export interface BranchConditionBlock {
  id: string; kind: 'branch_condition'; condition: string; outcome: string
}
export interface SampleFileBlock {
  id: string; kind: 'sample_file'; file_id?: string; filename?: string; mime?: string
}
export interface NoteBlock {
  id: string; kind: 'note'; text: string
}

// Descriptive annotation on expected_document nodes — declares fields present in the document
// (for the agent to recognise / confirm exist), not to extract values.
export interface DocFieldBlock {
  id: string; kind: 'doc_field'
  name: string                               // the field label, e.g. "Batch No"
  appears_as?: string                        // how it looks on the doc, e.g. "FEI Reg.No in line-item desc"
  scope?: 'line_item' | 'document'           // default: 'document'
}

export type Block =
  | RequiredFieldBlock | MatchKeyBlock | CountKeyBlock
  | BranchConditionBlock | SampleFileBlock | NoteBlock | DocFieldBlock

export interface NodeConfig {
  blocks?: Block[]
  // Extract & Validate — typed fail condition
  fail_if?: 'any_missing' | 'all_missing' | 'custom'
  custom_expr?: string
  // Scope — iteration declaration
  iterate_over?: string
  item_name?: string
  // S4.4 — universal: step intent (AI-readable; distinct from /note blocks)
  description?: string
  // S4.4 — Extract & Validate: iteration grain
  applies_to?: 'per_line_item' | 'per_document'
  // S4.4 — Match Documents: key-comparison strategy
  match_type?: 'exact' | 'normalized' | 'fuzzy'
  // S4.4 — expected_document: how the document is located
  identified_by?: 'header_text' | 'filename' | 'content'
  identifier?: string
  // S4.5 — Summarize: what to summarize + optional focus instructions
  summarize_source?: string
  summarize_instructions?: string
  // S4.5 — Scope variant: for_each (default) or custom free-text container
  scope_kind?: 'for_each' | 'custom'
  scope_label?: string
  // S5a.1 — Tool action: typed action category + free-text target system
  action_type?: 'fetch' | 'send' | 'call_api' | 'store' | 'transform' | 'other'
  action_target?: string
}

export interface NodeData extends Record<string, unknown> {
  kind: NodeKind
  title: string
  config: NodeConfig
}

export interface EdgeData extends Record<string, unknown> {
  edgeKind: EdgeKind
  label?: string
}

export interface BoardMeta {
  subject_name?: string
  key_field?: string
}

export interface Board {
  id: string
  name: string
  meta?: BoardMeta
  created_at?: string
  updated_at?: string
}

// ── Node kind metadata ────────────────────────────────────────────────────

// Canonical "building block" process node kinds (used for BLOCK_AVAILABILITY).
// Does NOT include aggregate (legacy), scope, custom, assumption (palette-separate).
export const NODE_KINDS: NodeKind[] = [
  'trigger', 'expected_document', 'extract_validate', 'match_documents',
  'decision', 'tool_action', 'report', 'count', 'summarize',
]

// S4.5: plain-language label renames — kind IDs unchanged so saved boards load without migration.
// aggregate stays as a valid legacy kind displayed as "Count".
export const NODE_LABELS: Record<NodeKind, string> = {
  trigger:           'Trigger',
  expected_document: 'Sample Document / File',
  extract_validate:  'Check a document',
  match_documents:   'Match up documents',
  decision:          'Decision',
  aggregate:         'Count',               // legacy alias — displays same as count
  count:             'Count',
  summarize:         'Summarize',
  tool_action:       'Take an action',
  report:            'Report results',
  custom:            'Custom',
  scope:             'Repeat for each',
  assumption:        'Assumption',
}

// S4.5: one plain-language sentence per kind for palette hover tooltips.
export const NODE_DESCRIPTIONS: Record<NodeKind, string> = {
  trigger:           "What starts this process (e.g. an email arrives).",
  expected_document: "A document or file the process works with (invoice, COA, spreadsheet…).",
  extract_validate:  "Read a document and make sure the required information is there.",
  match_documents:   "Cross-check two sets of documents against each other (e.g. match a batch to its certificate).",
  decision:          "A fork: go one way or another depending on what's true.",
  aggregate:         "Tally a collection into numbers (how many passed, failed, processed).",
  count:             "Tally a collection into numbers (how many passed, failed, processed).",
  summarize:         "Read content and write a short summary of it.",
  tool_action:       "Do something in the outside world (send an email, call a system).",
  report:            "Produce the final result or a record of what was found.",
  custom:            "Define your own step when nothing else fits — describe it in your own words.",
  scope:             "Run the steps inside this box once for every item (each invoice, each batch…).",
  assumption:        "Something you're taking for granted. The AI may question it later.",
}

// S4.5: palette groups drive the grouped sidebar UI. Containers/scope handled separately.
export const PALETTE_GROUPS: { label: string; kinds: NodeKind[] }[] = [
  { label: 'When something happens',  kinds: ['trigger'] },
  { label: 'Documents & information', kinds: ['expected_document'] },
  { label: 'Checks & reading',        kinds: ['extract_validate', 'match_documents'] },
  { label: 'Actions & decisions',     kinds: ['tool_action', 'decision', 'report'] },
  { label: 'Counting & looping',      kinds: ['count', 'summarize'] },
  { label: 'Notes & assumptions',     kinds: ['assumption'] },
]

export const NODE_ICONS: Record<NodeKind, string> = {
  trigger:           '⚡',
  expected_document: '📄',
  extract_validate:  '🔎',
  match_documents:   '🔗',
  decision:          '◇',
  aggregate:         'Σ',
  count:             'Σ',
  summarize:         '✍',
  tool_action:       '⚙',
  report:            '📊',
  custom:            '✦',
  scope:             '↻',
  assumption:        '?',
}

export const NODE_COLORS: Record<NodeKind, { bg: string; accent: string }> = {
  trigger:           { bg: '#1a1740', accent: '#818cf8' },
  expected_document: { bg: '#0f1e38', accent: '#60a5fa' },
  extract_validate:  { bg: '#0e2018', accent: '#4ade80' },
  match_documents:   { bg: '#0a1e1e', accent: '#34d399' },
  decision:          { bg: '#1e1030', accent: '#c084fc' },
  aggregate:         { bg: '#0e1c2c', accent: '#38bdf8' },
  count:             { bg: '#0e1c2c', accent: '#38bdf8' },
  summarize:         { bg: '#1a1330', accent: '#a78bfa' },
  tool_action:       { bg: '#1e1c08', accent: '#facc15' },
  report:            { bg: '#200e0e', accent: '#f87171' },
  custom:            { bg: '#131318', accent: '#9ca3af' },
  scope:             { bg: '#0e0e2e', accent: '#818cf8' },
  assumption:        { bg: '#1e1905', accent: '#fbbf24' },
}

export const EDGE_COLORS: Record<EdgeKind, string> = {
  default:   '#6b7280',
  on_pass:   '#22c55e',
  on_fail:   '#ef4444',
  exception: '#f59e0b',
  custom:    '#9ca3af',
}

// ── Block availability per node kind ─────────────────────────────────────

export const BLOCK_AVAILABILITY: Record<NodeKind, BlockKind[]> = {
  trigger:           ['sample_file', 'note'],
  expected_document: ['doc_field', 'sample_file', 'note'],
  extract_validate:  ['required_field', 'sample_file', 'note'],
  match_documents:   ['match_key', 'sample_file', 'note'],
  decision:          ['branch_condition', 'sample_file', 'note'],
  aggregate:         ['count_key', 'sample_file', 'note'],
  count:             ['count_key', 'sample_file', 'note'],
  summarize:         ['sample_file', 'note'],
  tool_action:       ['sample_file', 'note'],
  report:            ['sample_file', 'note'],
  custom:            ['required_field', 'match_key', 'count_key', 'branch_condition', 'sample_file', 'note'],
  scope:             [],
  assumption:        [],
}

// ── Gate comment (AI-check gate, S5a) ────────────────────────────────────

export interface GateComment {
  id: string
  board_id: string
  node_id: string | null
  severity: 'blocking' | 'advisory'
  status: 'open' | 'answered' | 'resolved' | 'rejected'
  question: string
  answer: string | null
  followup: string | null      // model follow-up when answer was insufficient
  parent_id: string | null     // links a follow-up to its parent comment
  resolved_at: string | null   // set when status → resolved
  round: number
  created_at: string
}

export interface FrozenSpec {
  board_id: string
  frozen_at: string
  spec: {
    board_id: string
    board_name: string
    frozen_at: string
    meta: { subject_name?: string; key_field?: string }
    nodes: unknown[]
    edges: unknown[]
    resolved_assumptions: Array<{
      comment_id: string
      node_id: string | null
      severity: string
      status: string
      round: number
      question: string
      answer: string | null
      followup: string | null
    }>
  }
}

// S5a.1 — tool_action typed action categories (label map is single source of truth).
export const ACTION_TYPE_LABELS: Record<NonNullable<NodeConfig['action_type']>, string> = {
  fetch:     'Read / Fetch data',
  send:      'Send / Notify',
  call_api:  'Call an API',
  store:     'Save / Store data',
  transform: 'Transform / Process data',
  other:     'Other',
}

// ── Live agent runs (Session 11a) ────────────────────────────────────────────

export interface AgentRunCounts {
  shipment_number:      string
  invoices_processed:   number
  invoices_succeeded:   number
  invoices_failed:      number
  goods_failed:         number
  batches_processed:    number
  batches_succeeded:    number
  batches_failed:       number
}

export interface AgentRun {
  id:           string
  board_id:     string
  message_id:   string
  subject:      string
  status:       'completed' | 'failed'
  csv_content:  string | null
  result_json:  AgentRunCounts | null
  created_at:   string
}

export interface FlowState {
  ai_check_done: boolean
  blocking_questions_open: number
  worked_example_captured: boolean
  agent_built: boolean
}

export const BLOCK_LABELS: Record<BlockKind, string> = {
  required_field:   '/ required field',
  match_key:        '/ match key',
  count_key:        '/ count key',
  branch_condition: '/ branch condition',
  sample_file:      '/ sample file',
  note:             '/ note',
  doc_field:        '/ field present',
}
