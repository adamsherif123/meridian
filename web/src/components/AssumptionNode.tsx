import { useContext } from 'react'
import type { NodeProps } from '@xyflow/react'
import { BoardContext } from '../context'
import type { NodeData } from '../types'

// Assumption is an ANNOTATION — not a flow step.
// No source/target handles: it intentionally cannot be wired into the control flow.
// Double-click opens NodeEditorModal (handled at ReactFlow level in CanvasPage).

export function AssumptionNode({ id, data, selected }: NodeProps) {
  const { title } = data as NodeData
  const board = useContext(BoardContext)!

  return (
    <div
      style={{
        background: '#1e1905',
        border: `1.5px ${selected ? 'solid #fbbf24' : 'dashed #92400e'}`,
        borderRadius: 8,
        padding: '8px 10px 11px',
        minWidth: 160,
        maxWidth: 240,
        boxShadow: selected
          ? '0 0 0 2px #fbbf2440, 0 3px 10px #0008'
          : '0 2px 8px #0006',
        transition: 'border-color 0.12s, box-shadow 0.12s',
        cursor: 'default',
      }}
    >
      {/* Header: kind label + delete */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 6 }}>
        <span style={{
          fontSize: 10, color: '#92400e',
          fontWeight: 800, textTransform: 'uppercase',
          letterSpacing: '0.08em', flex: 1,
        }}>
          ? Assumption
        </span>
        <button
          title="Delete"
          onClick={() => board.deleteNode(id)}
          onMouseDown={e => e.stopPropagation()}
          style={{
            background: 'none', border: 'none', color: '#78350f',
            cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: 0,
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
          onMouseLeave={e => (e.currentTarget.style.color = '#78350f')}
        >
          ×
        </button>
      </div>

      {/* Assumption statement (the title) */}
      <div
        title="Double-click to edit"
        style={{
          fontSize: 12,
          color: title ? '#fde68a' : '#78350f',
          fontStyle: title ? 'normal' : 'italic',
          lineHeight: 1.45,
          wordBreak: 'break-word',
        }}
      >
        {title || 'Double-click to add assumption…'}
      </div>
    </div>
  )
}
