import { useContext } from 'react'
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from '@xyflow/react'
import { BoardContext } from '../context'
import { EDGE_COLORS, type EdgeData, type EdgeKind } from '../types'

export function TypedEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  markerEnd,
}: EdgeProps) {
  const board = useContext(BoardContext)!
  const ed = data as EdgeData | undefined
  const edgeKind: EdgeKind = ed?.edgeKind ?? 'default'
  const isCustom = edgeKind === 'custom'
  const color = EDGE_COLORS[edgeKind]

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  return (
    <>
      <BaseEdge
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: color,
          strokeWidth: isCustom ? 1.5 : 2,
          strokeDasharray: isCustom ? '6 3' : undefined,
        }}
      />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan"
          style={{
            position: 'absolute',
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            pointerEvents: 'all',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 3,
          }}
        >
          <select
            value={edgeKind}
            onChange={e => board.changeEdgeKind(id, e.target.value as EdgeKind)}
            onClick={e => e.stopPropagation()}
            style={{
              background: '#111827',
              border: `1px solid ${color}`,
              color,
              borderRadius: 4,
              padding: '2px 6px',
              fontSize: 11,
              fontWeight: 600,
              cursor: 'pointer',
              outline: 'none',
            }}
          >
            <option value="default">→ default</option>
            <option value="on_pass">✓ on_pass</option>
            <option value="on_fail">✗ on_fail</option>
            <option value="exception">⚠ exception</option>
            <option value="custom">✦ custom</option>
          </select>

          {isCustom && (
            <input
              value={ed?.label ?? ''}
              onChange={e => board.changeEdgeLabel(id, e.target.value)}
              onClick={e => e.stopPropagation()}
              onKeyDown={e => e.stopPropagation()}
              placeholder="label…"
              className="nodrag nopan"
              style={{
                width: 104,
                background: '#111827',
                border: '1px solid #374151',
                color: '#d1d5db',
                borderRadius: 4,
                padding: '2px 6px',
                fontSize: 11,
                outline: 'none',
                textAlign: 'center',
              }}
            />
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  )
}
