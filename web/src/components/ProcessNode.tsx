import { useContext } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { BoardContext } from '../context'
import { ACTION_TYPE_LABELS, NODE_COLORS, NODE_ICONS, NODE_LABELS, type NodeConfig, type NodeData } from '../types'

// ── Extract & Validate — typed fail condition panel ───────────────────────
// Exported so NodeEditorModal can reuse it without duplicating the logic.

export function FailConditionPanel({ nodeId, config }: { nodeId: string; config: NodeConfig }) {
  const board = useContext(BoardContext)!
  const failIf = config.fail_if ?? 'any_missing'

  return (
    <div style={{ borderTop: '1px solid #111827', marginTop: 8, padding: '7px 0 4px' }}>
      <div style={{ fontSize: 9, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 700, marginBottom: 6 }}>
        Fail Condition
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 11, color: '#6b7280', whiteSpace: 'nowrap' }}>fail if</span>
        <select
          value={failIf}
          onChange={e => board.updateNodeConfig(nodeId, { fail_if: e.target.value as NodeConfig['fail_if'] })}
          onKeyDown={e => e.stopPropagation()}
          style={{
            background: '#0f172a', border: '1px solid #1f2937',
            color: '#d1d5db', borderRadius: 3, fontSize: 11, padding: '2px 4px', flex: 1,
          }}
        >
          <option value="any_missing">any required field is missing</option>
          <option value="all_missing">all required fields are missing</option>
          <option value="custom">custom expression</option>
        </select>
      </div>
      {failIf === 'custom' && (
        <input
          value={config.custom_expr ?? ''}
          onChange={e => board.updateNodeConfig(nodeId, { custom_expr: e.target.value })}
          onKeyDown={e => e.stopPropagation()}
          placeholder="expression…"
          style={{
            marginTop: 5, width: '100%', background: 'transparent',
            border: '1px solid #1f2937', borderRadius: 3, color: '#d1d5db',
            fontSize: 11, padding: '3px 6px', outline: 'none', boxSizing: 'border-box',
            fontFamily: 'inherit',
          }}
        />
      )}
    </div>
  )
}

// ── Process node (canvas representation — collapsed only) ─────────────────
// Double-clicking opens NodeEditorModal (handled at ReactFlow level in CanvasPage).
// All block editing, title editing, and typed config happens in the modal.

export function ProcessNode({ id, data, selected }: NodeProps) {
  const { kind, title, config } = data as NodeData
  const board = useContext(BoardContext)!
  const { bg, accent } = NODE_COLORS[kind]

  return (
    <div
      style={{
        background: bg,
        border: `1.5px solid ${selected ? '#fff' : accent}`,
        borderRadius: 8,
        minWidth: 172,
        maxWidth: 224,
        padding: '10px 12px 12px',
        boxShadow: selected ? `0 0 0 2px ${accent}55` : '0 2px 8px #0006',
        transition: 'border-color 0.12s, box-shadow 0.12s',
        cursor: 'default',
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{ background: accent, border: 'none', width: 10, height: 10 }}
      />

      {/* Kind header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <span style={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>{NODE_ICONS[kind]}</span>
        <span style={{
          fontSize: 10, color: accent, textTransform: 'uppercase',
          letterSpacing: '0.08em', fontWeight: 700, flex: 1,
          overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
        }}>
          {NODE_LABELS[kind]}
        </span>

        {/* Delete */}
        <button
          title="Delete node"
          onClick={() => board.deleteNode(id)}
          onMouseDown={e => e.stopPropagation()}
          style={{
            background: 'none', border: 'none', color: '#4b5563',
            cursor: 'pointer', fontSize: 18, lineHeight: 1,
            padding: '0 1px', borderRadius: 3, transition: 'color 0.1s', flexShrink: 0,
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
          onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
        >
          ×
        </button>
      </div>

      {/* Title (read-only on canvas — edit inside modal) */}
      <div
        title="Double-click to edit"
        style={{
          fontSize: 13, fontWeight: 500,
          color: title ? '#f1f5f9' : '#4b5563',
          padding: '4px 7px', borderRadius: 4,
          minHeight: 28, wordBreak: 'break-word', lineHeight: 1.4,
          border: '1px solid transparent',
        }}
      >
        {title || <span style={{ fontStyle: 'italic', color: '#4b5563' }}>Untitled</span>}
      </div>

      {/* Action-type badge — tool_action only */}
      {kind === 'tool_action' && (() => {
        const cfg = config as NodeConfig
        const at = cfg?.action_type
        if (!at || (at === 'other' && !cfg?.action_target)) return null
        return (
          <div style={{
            fontSize: 10, marginTop: 4, paddingLeft: 7,
            display: 'flex', gap: 4, alignItems: 'baseline',
            overflow: 'hidden',
          }}>
            <span style={{ color: `${accent}90`, whiteSpace: 'nowrap' }}>
              {ACTION_TYPE_LABELS[at]}
            </span>
            {cfg.action_target && (
              <span style={{
                color: '#4b5563', whiteSpace: 'nowrap',
                overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                · {cfg.action_target}
              </span>
            )}
          </div>
        )
      })()}

      <Handle
        type="source"
        position={Position.Right}
        style={{ background: accent, border: 'none', width: 10, height: 10 }}
      />
    </div>
  )
}
