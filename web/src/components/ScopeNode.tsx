import { useContext } from 'react'
import { NodeResizer, type NodeProps } from '@xyflow/react'
import { BoardContext } from '../context'
import type { NodeData } from '../types'

export function ScopeNode({ id, data, selected }: NodeProps) {
  const { config } = data as NodeData
  const board = useContext(BoardContext)!

  const scopeKind = config.scope_kind ?? 'for_each'
  const iterate_over = config.iterate_over ?? ''
  const item_name = config.item_name ?? ''
  const scopeLabel = config.scope_label ?? ''

  const isCustom = scopeKind === 'custom'
  const borderColor = selected ? (isCustom ? '#6ee7b7' : '#a5b4fc') : (isCustom ? '#065f46' : '#4f46e5')
  const headerBg = isCustom ? 'rgba(6,30,22,0.90)' : 'rgba(15,10,50,0.85)'
  const accentColor = isCustom ? '#34d399' : '#818cf8'

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <NodeResizer
        isVisible={selected}
        minWidth={220}
        minHeight={120}
        color={accentColor}
        lineStyle={{ borderColor: accentColor, borderWidth: 1 }}
        handleStyle={{ background: accentColor, border: `1px solid ${accentColor}`, width: 8, height: 8, borderRadius: 2 }}
      />

      {/* Dashed container border */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          border: `2px dashed ${borderColor}`,
          borderRadius: 12,
          background: isCustom
            ? `rgba(6,95,70,${selected ? '0.07' : '0.04'})`
            : `rgba(79,70,229,${selected ? '0.07' : '0.04'})`,
          transition: 'border-color 0.12s, background 0.12s',
          pointerEvents: 'none',
        }}
      />

      {/* Header bar */}
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 40,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '0 10px',
          background: headerBg,
          borderRadius: '10px 10px 0 0',
          borderBottom: `1px solid ${borderColor}40`,
        }}
      >
        {isCustom ? (
          /* ── Custom scope header ── */
          <>
            <span style={{ fontSize: 13, color: accentColor, flexShrink: 0, lineHeight: 1 }}>◻</span>
            <span style={{ fontSize: 10, color: accentColor, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0 }}>
              Group
            </span>
            <input
              value={scopeLabel}
              onChange={e => board.updateNodeConfig(id, { scope_label: e.target.value })}
              onKeyDown={e => e.stopPropagation()}
              placeholder="Describe what this group does…"
              className="nodrag nopan"
              style={{
                background: 'transparent',
                border: '1px solid #065f46',
                borderRadius: 4,
                color: '#6ee7b7',
                fontSize: 11,
                padding: '2px 6px',
                outline: 'none',
                flex: 1,
                minWidth: 80,
              }}
            />
          </>
        ) : (
          /* ── For-each scope header ── */
          <>
            <span style={{ fontSize: 13, color: '#818cf8', flexShrink: 0, lineHeight: 1 }}>↻</span>
            <span style={{ fontSize: 10, color: '#818cf8', fontWeight: 600, letterSpacing: '0.02em', flexShrink: 0 }}>
              Do this for every
            </span>
            <input
              value={item_name}
              onChange={e => board.updateNodeConfig(id, { item_name: e.target.value })}
              onKeyDown={e => e.stopPropagation()}
              placeholder="what?"
              className="nodrag nopan"
              style={{
                background: 'rgba(99,102,241,0.18)',
                border: '1px solid #6366f1',
                borderRadius: 4,
                color: '#c7d2fe',
                fontSize: 11,
                fontWeight: 600,
                padding: '2px 6px',
                outline: 'none',
                width: 72,
                flexShrink: 0,
              }}
            />
            <span style={{ fontSize: 10, color: '#4b5563', flexShrink: 0 }}>in</span>
            <input
              value={iterate_over}
              onChange={e => board.updateNodeConfig(id, { iterate_over: e.target.value })}
              onKeyDown={e => e.stopPropagation()}
              placeholder="from where?"
              className="nodrag nopan"
              style={{
                background: 'transparent',
                border: '1px solid #374151',
                borderRadius: 4,
                color: '#9ca3af',
                fontSize: 11,
                padding: '2px 6px',
                outline: 'none',
                flex: 1,
                minWidth: 60,
              }}
            />
          </>
        )}

        {/* Delete */}
        <button
          title="Delete scope"
          onClick={() => board.deleteNode(id)}
          onMouseDown={e => e.stopPropagation()}
          style={{
            background: 'none', border: 'none', color: '#374151',
            cursor: 'pointer', fontSize: 18, lineHeight: 1,
            padding: '0 1px', borderRadius: 3, flexShrink: 0,
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
          onMouseLeave={e => (e.currentTarget.style.color = '#374151')}
        >
          ×
        </button>
      </div>
    </div>
  )
}
