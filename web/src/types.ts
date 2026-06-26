export type NodeKind =
  | 'trigger'
  | 'expected_document'
  | 'extract_validate'
  | 'match_documents'
  | 'decision'
  | 'aggregate'
  | 'tool_action'
  | 'report'
  | 'custom'

export type EdgeKind = 'default' | 'on_pass' | 'on_fail' | 'exception' | 'custom'

export interface NodeData extends Record<string, unknown> {
  kind: NodeKind
  title: string
  config: Record<string, unknown>
}

export interface EdgeData extends Record<string, unknown> {
  edgeKind: EdgeKind
  label?: string  // user-supplied free text; only shown when edgeKind === 'custom'
}

export interface Board {
  id: string
  name: string
  created_at?: string
  updated_at?: string
}

// The 8 typed primitives — custom is handled separately in the palette.
export const NODE_KINDS: NodeKind[] = [
  'trigger',
  'expected_document',
  'extract_validate',
  'match_documents',
  'decision',
  'aggregate',
  'tool_action',
  'report',
]

export const NODE_LABELS: Record<NodeKind, string> = {
  trigger: 'Trigger',
  expected_document: 'Expected Document',
  extract_validate: 'Extract & Validate',
  match_documents: 'Match Documents',
  decision: 'Decision',
  aggregate: 'Aggregate / Summarize',
  tool_action: 'Tool Action',
  report: 'Report',
  custom: 'Custom',
}

export const NODE_ICONS: Record<NodeKind, string> = {
  trigger: '⚡',
  expected_document: '📄',
  extract_validate: '🔎',
  match_documents: '🔗',
  decision: '◇',
  aggregate: 'Σ',
  tool_action: '⚙',
  report: '📊',
  custom: '✦',
}

export const NODE_COLORS: Record<NodeKind, { bg: string; accent: string }> = {
  trigger:           { bg: '#1a1740', accent: '#818cf8' },
  expected_document: { bg: '#0f1e38', accent: '#60a5fa' },
  extract_validate:  { bg: '#0e2018', accent: '#4ade80' },
  match_documents:   { bg: '#0a1e1e', accent: '#34d399' },
  decision:          { bg: '#1e1030', accent: '#c084fc' },
  aggregate:         { bg: '#0e1c2c', accent: '#38bdf8' },
  tool_action:       { bg: '#1e1c08', accent: '#facc15' },
  report:            { bg: '#200e0e', accent: '#f87171' },
  // Intentionally neutral — signals "not a typed primitive"
  custom:            { bg: '#131318', accent: '#9ca3af' },
}

export const EDGE_COLORS: Record<EdgeKind, string> = {
  default:   '#6b7280',
  on_pass:   '#22c55e',
  on_fail:   '#ef4444',
  exception: '#f59e0b',
  // Intentionally neutral / lighter gray to read as "escape hatch"
  custom:    '#9ca3af',
}
