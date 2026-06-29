import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { Node } from '@xyflow/react'
import { BlockEditor } from './BlockEditor'
import { FailConditionPanel } from './ProcessNode'
import { BoardContext } from '../context'
import {
  ACTION_TYPE_LABELS, BLOCK_AVAILABILITY, NODE_COLORS, NODE_ICONS, NODE_LABELS,
  type NodeConfig, type NodeData, type NodeKind,
} from '../types'

// ── Action-type helpers (tool_action only) ────────────────────────────────

const ACTION_PREPOSITION: Record<NonNullable<NodeConfig['action_type']>, string> = {
  fetch:     'from',
  send:      'to',
  call_api:  '',
  store:     'to',
  transform: '',
  other:     '',
}

function buildSuggestedTitle(
  actionType: NodeConfig['action_type'] | undefined,
  target: string | undefined,
): string {
  if (!actionType) return ''
  const label = ACTION_TYPE_LABELS[actionType]
  const prep = ACTION_PREPOSITION[actionType]
  const t = target?.trim()
  if (t) return prep ? `${label} ${prep} ${t}` : `${label} — ${t}`
  return label
}

// ── Shared layout helpers ──────────────────────────────────────────────────

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 10, color: '#4b5563',
      textTransform: 'uppercase', letterSpacing: '0.08em',
      fontWeight: 700, marginBottom: 7,
    }}>
      {children}
    </div>
  )
}

function SectionDivider() {
  return <div style={{ borderTop: '1px solid #1e293b', margin: '18px 0 16px' }} />
}

function SelectRow({ label, value, onChange, options }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
      <span style={{ fontSize: 12, color: '#6b7280', minWidth: 100, flexShrink: 0 }}>{label}</span>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => e.stopPropagation()}
        style={{
          background: '#111827', border: '1px solid #374151',
          borderRadius: 5, color: '#d1d5db', fontSize: 12,
          padding: '5px 8px', outline: 'none', flex: 1, fontFamily: 'inherit',
          cursor: 'pointer',
        }}
        onFocus={e => (e.currentTarget.style.borderColor = '#6b7280')}
        onBlur={e => (e.currentTarget.style.borderColor = '#374151')}
      >
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  )
}

// ── Type-specific config panels ───────────────────────────────────────────
// Each panel uses BoardContext directly — no prop-threading needed.

function ArtifactConfigPanel({ nodeId, config }: { nodeId: string; config: NodeConfig }) {
  const board = useContext(BoardContext)!
  return (
    <>
      <Label>Document identification</Label>
      <SelectRow
        label="Identified by"
        value={config.identified_by ?? 'header_text'}
        onChange={v => board.updateNodeConfig(nodeId, { identified_by: v as NodeConfig['identified_by'] })}
        options={[
          { value: 'header_text', label: 'Header text' },
          { value: 'filename', label: 'Filename' },
          { value: 'content', label: 'Content match' },
        ]}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 12, color: '#6b7280', minWidth: 100, flexShrink: 0 }}>Identifier</span>
        <input
          value={config.identifier ?? ''}
          onChange={e => board.updateNodeConfig(nodeId, { identifier: e.target.value })}
          onKeyDown={e => e.stopPropagation()}
          placeholder={`e.g. "Certificate of Analysis"`}
          style={{
            background: '#111827', border: '1px solid #374151', borderRadius: 5,
            color: '#d1d5db', fontSize: 12, padding: '5px 8px', outline: 'none',
            flex: 1, boxSizing: 'border-box', fontFamily: 'inherit',
          }}
          onFocus={e => (e.currentTarget.style.borderColor = '#6b7280')}
          onBlur={e => (e.currentTarget.style.borderColor = '#374151')}
        />
      </div>
    </>
  )
}

function ExtractValidateConfigPanel({ nodeId, config }: { nodeId: string; config: NodeConfig }) {
  const board = useContext(BoardContext)!
  return (
    <>
      <Label>Extraction settings</Label>
      <SelectRow
        label="Applies to"
        value={config.applies_to ?? 'per_line_item'}
        onChange={v => board.updateNodeConfig(nodeId, { applies_to: v as NodeConfig['applies_to'] })}
        options={[
          { value: 'per_line_item', label: 'Per line item' },
          { value: 'per_document', label: 'Per document' },
        ]}
      />
      <FailConditionPanel nodeId={nodeId} config={config} />
    </>
  )
}

function MatchDocumentsConfigPanel({ nodeId, config }: { nodeId: string; config: NodeConfig }) {
  const board = useContext(BoardContext)!
  return (
    <>
      <Label>Matching strategy</Label>
      <SelectRow
        label="Match type"
        value={config.match_type ?? 'exact'}
        onChange={v => board.updateNodeConfig(nodeId, { match_type: v as NodeConfig['match_type'] })}
        options={[
          { value: 'exact', label: 'Exact' },
          { value: 'normalized', label: 'Normalized (ignore whitespace / case)' },
          { value: 'fuzzy', label: 'Fuzzy' },
        ]}
      />
    </>
  )
}

function SummarizeConfigPanel({ nodeId, config }: { nodeId: string; config: NodeConfig }) {
  const board = useContext(BoardContext)!
  return (
    <>
      <Label>Summarization settings</Label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <span style={{ fontSize: 12, color: '#6b7280', minWidth: 100, flexShrink: 0 }}>Source</span>
        <input
          value={config.summarize_source ?? ''}
          onChange={e => board.updateNodeConfig(nodeId, { summarize_source: e.target.value })}
          onKeyDown={e => e.stopPropagation()}
          placeholder="What to summarize (e.g. 'the incoming document')"
          style={{
            background: '#111827', border: '1px solid #374151', borderRadius: 5,
            color: '#d1d5db', fontSize: 12, padding: '5px 8px', outline: 'none',
            flex: 1, boxSizing: 'border-box', fontFamily: 'inherit',
          }}
          onFocus={e => (e.currentTarget.style.borderColor = '#6b7280')}
          onBlur={e => (e.currentTarget.style.borderColor = '#374151')}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <span style={{ fontSize: 12, color: '#6b7280', minWidth: 100, flexShrink: 0, paddingTop: 7 }}>Instructions</span>
        <textarea
          value={config.summarize_instructions ?? ''}
          onChange={e => board.updateNodeConfig(nodeId, { summarize_instructions: e.target.value })}
          onKeyDown={e => e.stopPropagation()}
          rows={2}
          placeholder="How to summarize, what to focus on…"
          style={{
            background: '#111827', border: '1px solid #374151', borderRadius: 5,
            color: '#d1d5db', fontSize: 12, padding: '5px 8px', outline: 'none',
            flex: 1, boxSizing: 'border-box', fontFamily: 'inherit', resize: 'vertical',
          }}
          onFocus={e => (e.currentTarget.style.borderColor = '#6b7280')}
          onBlur={e => (e.currentTarget.style.borderColor = '#374151')}
        />
      </div>
    </>
  )
}

function ToolActionConfigPanel({
  nodeId, config, onAutoTitle,
}: {
  nodeId: string
  config: NodeConfig
  onAutoTitle: (suggested: string) => void
}) {
  const board = useContext(BoardContext)!
  return (
    <>
      <Label>Action type</Label>
      <SelectRow
        label="Action type"
        value={config.action_type ?? ''}
        onChange={v => {
          const newType = (v || undefined) as NodeConfig['action_type']
          board.updateNodeConfig(nodeId, { action_type: newType })
          onAutoTitle(buildSuggestedTitle(newType, config.action_target))
        }}
        options={[
          { value: '', label: '— choose —' },
          ...Object.entries(ACTION_TYPE_LABELS).map(([v, l]) => ({ value: v, label: l })),
        ]}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 12, color: '#6b7280', minWidth: 100, flexShrink: 0 }}>Target / system</span>
        <input
          value={config.action_target ?? ''}
          onChange={e => {
            const newTarget = e.target.value
            board.updateNodeConfig(nodeId, { action_target: newTarget })
            if (config.action_type) {
              onAutoTitle(buildSuggestedTitle(config.action_type, newTarget))
            }
          }}
          onKeyDown={e => e.stopPropagation()}
          placeholder="What system does this act on? (e.g. Gmail inbox, Slack, a database…)"
          style={{
            background: '#111827', border: '1px solid #374151', borderRadius: 5,
            color: '#d1d5db', fontSize: 12, padding: '5px 8px', outline: 'none',
            flex: 1, boxSizing: 'border-box', fontFamily: 'inherit',
          }}
          onFocus={e => (e.currentTarget.style.borderColor = '#6b7280')}
          onBlur={e => (e.currentTarget.style.borderColor = '#374151')}
        />
      </div>
    </>
  )
}

// ── Main modal ────────────────────────────────────────────────────────────

interface NodeEditorModalProps {
  node: Node
  onClose: () => void
}

export function NodeEditorModal({ node, onClose }: NodeEditorModalProps) {
  const board = useContext(BoardContext)!
  const { kind, title, config } = node.data as NodeData
  const cfg = (config ?? {}) as NodeConfig
  const { accent } = NODE_COLORS[kind as NodeKind] ?? { accent: '#818cf8' }

  // Local draft states: committed on blur/Enter so typing doesn't race with node re-renders
  const [draftTitle, setDraftTitle] = useState(title)
  // Description commits live (small enough that keystroke-level saves are fine)
  const [draftDesc, setDraftDesc] = useState(cfg.description ?? '')

  const titleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    titleRef.current?.focus()
    titleRef.current?.select()
  }, [])

  const handleGlobalKey = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
  }, [onClose])

  useEffect(() => {
    window.addEventListener('keydown', handleGlobalKey)
    return () => window.removeEventListener('keydown', handleGlobalKey)
  }, [handleGlobalKey])

  function commitTitle() {
    const next = draftTitle.trim()
    if (next) board.updateNodeTitle(node.id, next)
  }

  function handleDescChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const v = e.target.value
    setDraftDesc(v)
    board.updateNodeConfig(node.id, { description: v })
  }

  // blocks come from the live node prop (re-passed by CanvasPage on every nodes-state change)
  const blocks = cfg.blocks ?? []

  const hasTypeConfig = ['expected_document', 'extract_validate', 'match_documents', 'summarize', 'tool_action'].includes(kind)

  // Auto-fill title from action_type + target only when title is still the default.
  function handleAutoTitle(suggested: string) {
    const isDefault = !draftTitle.trim() || draftTitle === NODE_LABELS['tool_action']
    if (isDefault && suggested) {
      setDraftTitle(suggested)
      board.updateNodeTitle(node.id, suggested)
    }
  }
  const hasBlocks = BLOCK_AVAILABILITY[kind as NodeKind]?.length > 0

  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        zIndex: 500,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(3,7,18,0.80)',
        backdropFilter: 'blur(3px)',
      }}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
      onWheel={e => e.stopPropagation()}
    >
      <div
        style={{
          background: '#0f172a',
          border: `1px solid ${accent}40`,
          borderRadius: 14,
          width: 'min(720px, 92vw)',
          maxHeight: '86vh',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          boxShadow: `0 0 0 1px ${accent}18, 0 32px 80px rgba(0,0,0,0.72)`,
        }}
        onMouseDown={e => e.stopPropagation()}
      >

        {/* ── Modal header ─────────────────────────────────────────── */}
        <div style={{
          padding: '14px 22px 12px',
          borderBottom: '1px solid #1e293b',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          flexShrink: 0,
          background: '#0d1424',
        }}>
          <span style={{ fontSize: 21, lineHeight: 1 }}>{NODE_ICONS[kind as NodeKind]}</span>
          <span style={{
            fontSize: 11, color: accent,
            textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 700,
          }}>
            {NODE_LABELS[kind as NodeKind]}
          </span>
          <div style={{ flex: 1 }} />
          <button
            onClick={onClose}
            title="Close (Esc)"
            style={{
              background: 'none', border: 'none', color: '#4b5563',
              cursor: 'pointer', fontSize: 24, lineHeight: 1, padding: '0 2px',
            }}
            onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
            onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
          >×</button>
        </div>

        {/* ── Scrollable body ──────────────────────────────────────── */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '22px 26px 36px' }}>

          {/* ── Title ── */}
          <div style={{ marginBottom: 18 }}>
            <Label>Title</Label>
            <input
              ref={titleRef}
              value={draftTitle}
              onChange={e => setDraftTitle(e.target.value)}
              onKeyDown={e => {
                e.stopPropagation()
                if (e.key === 'Enter') { commitTitle(); e.currentTarget.blur() }
              }}
              placeholder="Node title…"
              style={{
                width: '100%',
                background: '#111827',
                border: `1px solid ${accent}44`,
                borderRadius: 8,
                color: '#f9fafb',
                fontSize: 18,
                fontWeight: 600,
                padding: '9px 13px',
                outline: 'none',
                boxSizing: 'border-box',
                fontFamily: 'inherit',
              }}
              onFocus={e => (e.currentTarget.style.borderColor = accent)}
              onBlur={e => { e.currentTarget.style.borderColor = `${accent}44`; commitTitle() }}
            />
          </div>

          {/* ── Description ── */}
          <div style={{ marginBottom: 6 }}>
            <Label>What this step does</Label>
            <textarea
              value={draftDesc}
              onChange={handleDescChange}
              onKeyDown={e => e.stopPropagation()}
              rows={3}
              placeholder="Describe this step's purpose in plain language. The AI agent will read this to understand intent."
              style={{
                width: '100%',
                background: '#111827',
                border: '1px solid #1e293b',
                borderRadius: 8,
                color: '#d1d5db',
                fontSize: 13,
                padding: '9px 13px',
                outline: 'none',
                boxSizing: 'border-box',
                fontFamily: 'inherit',
                resize: 'vertical',
                lineHeight: 1.6,
              }}
              onFocus={e => (e.currentTarget.style.borderColor = '#374151')}
              onBlur={e => (e.currentTarget.style.borderColor = '#1e293b')}
            />
          </div>

          {/* ── Type-specific config ── */}
          {hasTypeConfig && <SectionDivider />}

          {kind === 'expected_document' && (
            <ArtifactConfigPanel nodeId={node.id} config={cfg} />
          )}
          {kind === 'extract_validate' && (
            <ExtractValidateConfigPanel nodeId={node.id} config={cfg} />
          )}
          {kind === 'match_documents' && (
            <MatchDocumentsConfigPanel nodeId={node.id} config={cfg} />
          )}
          {kind === 'summarize' && (
            <SummarizeConfigPanel nodeId={node.id} config={cfg} />
          )}
          {kind === 'tool_action' && (
            <ToolActionConfigPanel nodeId={node.id} config={cfg} onAutoTitle={handleAutoTitle} />
          )}

          {/* ── Blocks ── */}
          {hasBlocks && (
            <>
              <SectionDivider />
              <Label>Structured fields &amp; notes</Label>
              <BlockEditor
                nodeId={node.id}
                nodeKind={kind as NodeKind}
                blocks={blocks}
                showAddButtons
                showReorder
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
