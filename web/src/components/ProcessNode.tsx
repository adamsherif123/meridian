import { useContext, useRef, useState } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { BoardContext } from '../context'
import { NODE_COLORS, NODE_ICONS, NODE_LABELS, type NodeData } from '../types'

export function ProcessNode({ id, data, selected }: NodeProps) {
  const { kind, title } = data as NodeData
  const board = useContext(BoardContext)!
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const { bg, accent } = NODE_COLORS[kind]

  function startEdit() {
    setDraft(title)
    setEditing(true)
  }

  function commitTitle() {
    const next = draft.trim()
    if (next) board.updateNodeTitle(id, next)
    setEditing(false)
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') commitTitle()
    if (e.key === 'Escape') setEditing(false)
    e.stopPropagation()
  }

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
        <span
          style={{
            fontSize: 10,
            color: accent,
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            fontWeight: 700,
            flex: 1,
            overflow: 'hidden',
            whiteSpace: 'nowrap',
            textOverflow: 'ellipsis',
          }}
        >
          {NODE_LABELS[kind]}
        </span>
        <button
          title="Delete node"
          onClick={() => board.deleteNode(id)}
          onMouseDown={e => e.stopPropagation()}
          style={{
            background: 'none',
            border: 'none',
            color: '#4b5563',
            cursor: 'pointer',
            fontSize: 18,
            lineHeight: 1,
            padding: '0 1px',
            borderRadius: 3,
            transition: 'color 0.1s',
            flexShrink: 0,
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
          onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
        >
          ×
        </button>
      </div>

      {/* Editable title */}
      {editing ? (
        <input
          ref={inputRef}
          autoFocus
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onBlur={commitTitle}
          onKeyDown={onKeyDown}
          className="nodrag nopan"
          style={{
            width: '100%',
            background: '#0f172a',
            border: `1px solid ${accent}`,
            borderRadius: 4,
            color: '#f9fafb',
            fontSize: 13,
            fontWeight: 500,
            padding: '4px 7px',
            outline: 'none',
            boxSizing: 'border-box',
          }}
        />
      ) : (
        <div
          onDoubleClick={startEdit}
          title="Double-click to rename"
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: title ? '#f1f5f9' : '#6b7280',
            padding: '4px 7px',
            borderRadius: 4,
            border: '1px solid transparent',
            minHeight: 28,
            wordBreak: 'break-word',
            lineHeight: 1.4,
            transition: 'border-color 0.1s',
          }}
          onMouseEnter={e => (e.currentTarget.style.borderColor = '#374151')}
          onMouseLeave={e => (e.currentTarget.style.borderColor = 'transparent')}
        >
          {title || 'Double-click to rename'}
        </div>
      )}

      <Handle
        type="source"
        position={Position.Right}
        style={{ background: accent, border: 'none', width: 10, height: 10 }}
      />
    </div>
  )
}
