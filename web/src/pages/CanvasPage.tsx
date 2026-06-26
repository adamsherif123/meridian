import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  MarkerType,
  type Node,
  type Edge,
  type Connection,
  type NodeTypes,
  type EdgeTypes,
} from '@xyflow/react'

import { BoardContext } from '../context'
import { ProcessNode } from '../components/ProcessNode'
import { TypedEdge } from '../components/TypedEdge'
import {
  type Board,
  type EdgeKind,
  type NodeKind,
  NODE_ICONS,
  NODE_KINDS,
  NODE_LABELS,
} from '../types'

// Defined at module level so React Flow doesn't recreate on every render.
const nodeTypes: NodeTypes = { process: ProcessNode }
const edgeTypes: EdgeTypes = { typed: TypedEdge }

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

type SaveStatus = 'saved' | 'saving' | 'unsaved'

export default function CanvasPage() {
  const [boards, setBoards] = useState<Board[]>([])
  const [activeBoardId, setActiveBoardId] = useState<string | null>(null)
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('saved')
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const justLoadedRef = useRef(false)
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const activeBoardIdRef = useRef<string | null>(null)
  activeBoardIdRef.current = activeBoardId

  // ── Board context (stable — setNodes/setEdges never change) ──────────────
  const boardContext = useMemo(() => ({
    updateNodeTitle(id: string, title: string) {
      setNodes(nds => nds.map(n => n.id === id ? { ...n, data: { ...n.data, title } } : n))
    },
    deleteNode(id: string) {
      setNodes(nds => nds.filter(n => n.id !== id))
      setEdges(eds => eds.filter(e => e.source !== id && e.target !== id))
    },
    changeEdgeKind(id: string, kind: EdgeKind) {
      setEdges(eds => eds.map(e => e.id === id ? { ...e, data: { ...e.data, edgeKind: kind } } : e))
    },
    changeEdgeLabel(id: string, label: string) {
      setEdges(eds => eds.map(e => e.id === id ? { ...e, data: { ...e.data, label } } : e))
    },
  }), [setNodes, setEdges])

  // ── Load boards on mount ─────────────────────────────────────────────────
  useEffect(() => { loadBoards() }, [])

  async function loadBoards() {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards`)
      if (!res.ok) return
      const data: Board[] = await res.json()
      setBoards(data)
      if (data.length > 0) {
        setActiveBoardId(data[0].id)
        await loadBoardGraph(data[0].id)
      }
    } catch { /* backend may not be reachable */ }
  }

  async function loadBoardGraph(boardId: string) {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}`)
      if (!res.ok) return
      const data = await res.json()
      justLoadedRef.current = true
      setNodes(data.nodes ?? [])
      setEdges(data.edges ?? [])
    } catch { /* handled */ }
  }

  // ── Autosave on nodes/edges change ───────────────────────────────────────
  useEffect(() => {
    if (justLoadedRef.current) {
      justLoadedRef.current = false
      return
    }
    const boardId = activeBoardIdRef.current
    if (!boardId) return

    setSaveStatus('unsaved')
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => doSave(boardId, nodes, edges), 1400)
  }, [nodes, edges])  // eslint-disable-line react-hooks/exhaustive-deps

  async function doSave(boardId: string, currentNodes: Node[], currentEdges: Edge[]) {
    setSaveStatus('saving')
    try {
      await fetch(`${API_BASE}/api/v1/boards/${boardId}/graph`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: currentNodes, edges: currentEdges }),
      })
      setSaveStatus('saved')
    } catch {
      setSaveStatus('unsaved')
    }
  }

  // ── Board management ─────────────────────────────────────────────────────
  async function createBoard() {
    const name = `Board ${boards.length + 1}`
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!res.ok) return
      const board: Board = await res.json()
      setBoards(prev => [board, ...prev])
      setActiveBoardId(board.id)
      justLoadedRef.current = true
      setNodes([])
      setEdges([])
      setSaveStatus('saved')
    } catch { /* handled */ }
  }

  async function selectBoard(id: string) {
    if (id === activeBoardId) return
    setActiveBoardId(id)
    await loadBoardGraph(id)
    setSaveStatus('saved')
  }

  // ── Add node from palette ─────────────────────────────────────────────────
  function addNode(kind: NodeKind) {
    const newNode: Node = {
      id: crypto.randomUUID(),
      type: 'process',
      position: {
        x: 120 + Math.random() * 320,
        y: 80 + Math.random() * 240,
      },
      data: { kind, title: NODE_LABELS[kind], config: {} },
    }
    setNodes(nds => [...nds, newNode])
  }

  // ── Edge connection ───────────────────────────────────────────────────────
  const onConnect = useCallback((connection: Connection) => {
    setEdges(eds =>
      addEdge(
        {
          ...connection,
          type: 'typed',
          data: { edgeKind: 'default' },
          markerEnd: { type: MarkerType.ArrowClosed, color: '#6b7280' },
        },
        eds,
      ),
    )
  }, [setEdges])

  // ── Save indicator ────────────────────────────────────────────────────────
  const saveLabel =
    saveStatus === 'saved' ? '✓ Saved' :
    saveStatus === 'saving' ? '… Saving' :
    '● Unsaved'
  const saveColor =
    saveStatus === 'saved' ? '#22c55e' :
    saveStatus === 'saving' ? '#9ca3af' :
    '#f59e0b'

  return (
    <BoardContext.Provider value={boardContext}>
      <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: '#030712', color: '#f9fafb' }}>

        {/* ── Top bar ─────────────────────────────────────────────────── */}
        <div style={{
          height: 52,
          flexShrink: 0,
          borderBottom: '1px solid #1f2937',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          gap: 12,
          background: '#030712',
        }}>
          {/* Wordmark */}
          <span style={{ fontWeight: 800, fontSize: 16, letterSpacing: '-0.02em', color: '#f9fafb', marginRight: 8 }}>
            Meridian
          </span>

          {/* Divider */}
          <div style={{ width: 1, height: 24, background: '#1f2937' }} />

          {/* Board picker */}
          {boards.length > 0 ? (
            <select
              value={activeBoardId ?? ''}
              onChange={e => selectBoard(e.target.value)}
              style={{
                background: '#111827',
                border: '1px solid #374151',
                color: '#f9fafb',
                borderRadius: 6,
                padding: '4px 8px',
                fontSize: 13,
                outline: 'none',
                cursor: 'pointer',
              }}
            >
              {boards.map(b => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
          ) : (
            <span style={{ fontSize: 13, color: '#6b7280' }}>No boards yet</span>
          )}

          <button
            onClick={createBoard}
            style={{
              background: 'none',
              border: '1px solid #374151',
              color: '#9ca3af',
              borderRadius: 6,
              padding: '4px 10px',
              fontSize: 13,
              cursor: 'pointer',
              transition: 'color 0.1s, border-color 0.1s',
            }}
            onMouseEnter={e => { e.currentTarget.style.color = '#f9fafb'; e.currentTarget.style.borderColor = '#6b7280' }}
            onMouseLeave={e => { e.currentTarget.style.color = '#9ca3af'; e.currentTarget.style.borderColor = '#374151' }}
          >
            + New Board
          </button>

          {/* Spacer */}
          <div style={{ flex: 1 }} />

          {/* Save status */}
          {activeBoardId && (
            <span style={{ fontSize: 12, color: saveColor, fontWeight: 600 }}>{saveLabel}</span>
          )}

          {/* Nav tabs */}
          <div style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
            <span style={{
              fontSize: 13, padding: '4px 12px', borderRadius: 6,
              background: '#1e1b4b', border: '1px solid #4f46e5', color: '#f9fafb',
            }}>Canvas</span>
            <a
              href="#/skeleton"
              style={{
                fontSize: 13, padding: '4px 12px', borderRadius: 6,
                background: 'none', border: '1px solid #374151', color: '#6b7280',
                textDecoration: 'none', transition: 'color 0.1s, border-color 0.1s',
              }}
              onMouseEnter={e => { e.currentTarget.style.color = '#f9fafb'; e.currentTarget.style.borderColor = '#6b7280' }}
              onMouseLeave={e => { e.currentTarget.style.color = '#6b7280'; e.currentTarget.style.borderColor = '#374151' }}
            >
              Skeleton
            </a>
          </div>
        </div>

        {/* ── Body (palette + canvas) ───────────────────────────────────── */}
        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>

          {/* Palette */}
          <div style={{
            width: 192,
            flexShrink: 0,
            borderRight: '1px solid #1f2937',
            padding: '12px 8px',
            overflowY: 'auto',
            background: '#030712',
          }}>
            <div style={{ fontSize: 10, color: '#4b5563', textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 700, padding: '0 8px 8px' }}>
              Node types
            </div>
            {NODE_KINDS.map(kind => (
              <button
                key={kind}
                onClick={() => addNode(kind)}
                style={{
                  width: '100%',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '7px 10px',
                  marginBottom: 3,
                  background: 'none',
                  border: '1px solid transparent',
                  borderRadius: 6,
                  color: '#d1d5db',
                  fontSize: 12,
                  fontWeight: 500,
                  cursor: 'pointer',
                  textAlign: 'left',
                  transition: 'background 0.1s, border-color 0.1s, color 0.1s',
                }}
                onMouseEnter={e => {
                  e.currentTarget.style.background = '#111827'
                  e.currentTarget.style.borderColor = '#374151'
                  e.currentTarget.style.color = '#f9fafb'
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.background = 'none'
                  e.currentTarget.style.borderColor = 'transparent'
                  e.currentTarget.style.color = '#d1d5db'
                }}
              >
                <span style={{ fontSize: 15, width: 20, textAlign: 'center', flexShrink: 0 }}>
                  {NODE_ICONS[kind]}
                </span>
                {NODE_LABELS[kind]}
              </button>
            ))}

            {/* Escape-hatch divider */}
            <div style={{ borderTop: '1px solid #1f2937', margin: '8px 4px 6px' }} />
            <button
              onClick={() => addNode('custom')}
              style={{
                width: '100%',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '7px 10px',
                marginBottom: 3,
                background: 'none',
                border: '1px solid transparent',
                borderRadius: 6,
                color: '#6b7280',
                fontSize: 12,
                fontWeight: 500,
                cursor: 'pointer',
                textAlign: 'left',
                transition: 'background 0.1s, border-color 0.1s, color 0.1s',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background = '#111827'
                e.currentTarget.style.borderColor = '#374151'
                e.currentTarget.style.color = '#9ca3af'
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'none'
                e.currentTarget.style.borderColor = 'transparent'
                e.currentTarget.style.color = '#6b7280'
              }}
            >
              <span style={{ fontSize: 15, width: 20, textAlign: 'center', flexShrink: 0 }}>
                {NODE_ICONS['custom']}
              </span>
              {NODE_LABELS['custom']}
            </button>

            {/* Edge legend */}
            <div style={{ marginTop: 20, padding: '0 8px' }}>
              <div style={{ fontSize: 10, color: '#4b5563', textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 700, marginBottom: 8 }}>
                Edge types
              </div>
              {([
                ['default',   '#6b7280', '→ default',   false],
                ['on_pass',   '#22c55e', '✓ on_pass',   false],
                ['on_fail',   '#ef4444', '✗ on_fail',   false],
                ['exception', '#f59e0b', '⚠ exception', false],
                ['custom',    '#9ca3af', '✦ custom',    true],
              ] as const).map(([, color, label, dashed]) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, fontSize: 11 }}>
                  <div style={{
                    width: 24, height: 0, flexShrink: 0,
                    borderTop: dashed ? `2px dashed ${color}` : `2px solid ${color}`,
                  }} />
                  <span style={{ color: '#9ca3af' }}>{label}</span>
                </div>
              ))}
            </div>

            {!activeBoardId && (
              <div style={{ marginTop: 24, padding: '10px', background: '#111827', borderRadius: 6, fontSize: 11, color: '#6b7280', lineHeight: 1.5 }}>
                Create a board above to start building.
              </div>
            )}
          </div>

          {/* React Flow canvas */}
          <div style={{ flex: 1, position: 'relative' }}>
            {!activeBoardId && (
              <div style={{
                position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 10, pointerEvents: 'none',
              }}>
                <div style={{ textAlign: 'center', color: '#374151' }}>
                  <div style={{ fontSize: 48, marginBottom: 12 }}>◻</div>
                  <div style={{ fontSize: 16, fontWeight: 600, color: '#4b5563' }}>No board selected</div>
                  <div style={{ fontSize: 13, marginTop: 4 }}>Click "+ New Board" to get started</div>
                </div>
              </div>
            )}
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              colorMode="dark"
              fitView
              deleteKeyCode={['Backspace', 'Delete']}
              style={{ background: '#060d1a' }}
            >
              <Background color="#1f2937" gap={20} size={1} />
              <Controls style={{ background: '#111827', border: '1px solid #1f2937' }} />
              <MiniMap
                nodeColor={n => {
                  const kind = (n.data as { kind?: string })?.kind
                  return kind ? '#4f46e5' : '#374151'
                }}
                style={{ background: '#111827', border: '1px solid #1f2937' }}
              />
            </ReactFlow>
          </div>
        </div>
      </div>
    </BoardContext.Provider>
  )
}
