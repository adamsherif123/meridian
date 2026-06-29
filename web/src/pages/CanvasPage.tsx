import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  applyNodeChanges,
  useNodesState,
  useEdgesState,
  MarkerType,
  type Node,
  type Edge,
  type Connection,
  type NodeChange,
  type NodeTypes,
  type EdgeTypes,
} from '@xyflow/react'

import { BoardContext } from '../context'
import { AssumptionNode } from '../components/AssumptionNode'
import { BuildOrchestratorModal } from '../components/BuildOrchestratorModal'
import { GateBubble } from '../components/GateBubble'
import { GuidedPanel } from '../components/GuidedPanel'
import { NodeEditorModal } from '../components/NodeEditorModal'
import { ProcessNode } from '../components/ProcessNode'
import { ScopeNode } from '../components/ScopeNode'
import { TypedEdge } from '../components/TypedEdge'
import { WorkedExampleModal } from '../components/WorkedExampleModal'
import {
  type AgentRun,
  type Block,
  type Board,
  type BoardMeta,
  type EdgeKind,
  type FlowState,
  type FrozenSpec,
  type GateComment,
  type NodeConfig,
  type NodeKind,
  NODE_DESCRIPTIONS,
  NODE_ICONS,
  NODE_LABELS,
  PALETTE_GROUPS,
} from '../types'

// ── Module-level constants (React Flow requires these to be stable) ────────

const nodeTypes: NodeTypes = { process: ProcessNode, scope: ScopeNode, assumption: AssumptionNode }
const edgeTypes: EdgeTypes = { typed: TypedEdge }

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

type SaveStatus = 'saved' | 'saving' | 'unsaved'

// ── Node ordering helpers ─────────────────────────────────────────────────
// React Flow requires parents earlier in the nodes array than their children
// for correct z-ordering and position inheritance.
function sortForRender(nodes: Node[]): Node[] {
  const depthMap = new Map<string, number>()

  function nodeDepth(nodeId: string, visited = new Set<string>()): number {
    if (depthMap.has(nodeId)) return depthMap.get(nodeId)!
    if (visited.has(nodeId)) { depthMap.set(nodeId, 0); return 0 }  // cycle guard
    const n = nodes.find(x => x.id === nodeId)
    if (!n || !n.parentId) { depthMap.set(nodeId, 0); return 0 }
    visited.add(nodeId)
    const d = nodeDepth(n.parentId, visited) + 1
    depthMap.set(nodeId, d)
    return d
  }

  nodes.forEach(n => nodeDepth(n.id))

  return [...nodes].sort((a, b) => {
    const da = depthMap.get(a.id) ?? 0
    const db = depthMap.get(b.id) ?? 0
    if (da !== db) return da - db
    const as = a.type === 'scope' ? 0 : 1
    const bs = b.type === 'scope' ? 0 : 1
    return as - bs
  })
}

// ── Parent-assignment helpers ─────────────────────────────────────────────

function computeAbsPos(nodeId: string, nodes: Node[]): { x: number; y: number } {
  const n = nodes.find(x => x.id === nodeId)
  if (!n) return { x: 0, y: 0 }
  if (!n.parentId) return { ...n.position }
  const p = computeAbsPos(n.parentId, nodes)
  return { x: n.position.x + p.x, y: n.position.y + p.y }
}

function collectDescendants(id: string, nodes: Node[]): Set<string> {
  const result = new Set<string>([id])
  let changed = true
  while (changed) {
    changed = false
    for (const n of nodes) {
      if (n.parentId && result.has(n.parentId) && !result.has(n.id)) {
        result.add(n.id)
        changed = true
      }
    }
  }
  return result
}

// ── Component ─────────────────────────────────────────────────────────────

export default function CanvasPage() {
  const [boards, setBoards] = useState<Board[]>([])
  const [activeBoardId, setActiveBoardId] = useState<string | null>(null)
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null)
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('saved')
  const [gateComments, setGateComments] = useState<GateComment[]>([])
  const [gateRunning, setGateRunning] = useState(false)
  const [gateError, setGateError] = useState<string | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [frozenSpec, setFrozenSpec] = useState<FrozenSpec | null>(null)
  const [specOpen, setSpecOpen] = useState(false)
  const [flowState, setFlowState] = useState<FlowState | null>(null)
  const [showWorkedExampleModal, setShowWorkedExampleModal] = useState(false)
  const [showBuildOrchestrator, setShowBuildOrchestrator] = useState(false)
  // Run on inbox
  const [runLiveRunning, setRunLiveRunning] = useState(false)
  const [runLiveResult, setRunLiveResult] = useState<AgentRun | null>(null)
  const [runLiveError, setRunLiveError] = useState<string | null>(null)
  // Viewport state for pin-position math (updated via ReactFlow onMove)
  const [viewport, setViewport] = useState({ x: 0, y: 0, zoom: 1 })
  const [subject, setSubject] = useState<BoardMeta>({})
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const justLoadedRef = useRef(false)
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const subjectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const activeBoardIdRef = useRef<string | null>(null)
  activeBoardIdRef.current = activeBoardId
  const nodesRef = useRef<Node[]>([])
  nodesRef.current = nodes

  // ── Board context ─────────────────────────────────────────────────────────
  const boardContext = useMemo(() => ({
    activeBoardId,
    updateNodeTitle(id: string, title: string) {
      setNodes(nds => nds.map(n => n.id === id ? { ...n, data: { ...n.data, title } } : n))
    },
    deleteNode(id: string) {
      setNodes(nds => {
        const toDelete = collectDescendants(id, nds)
        return nds.filter(n => !toDelete.has(n.id))
      })
      setEdges(eds => eds.filter(e => e.source !== id && e.target !== id))
    },
    changeEdgeKind(id: string, kind: EdgeKind) {
      setEdges(eds => eds.map(e => e.id === id ? { ...e, data: { ...e.data, edgeKind: kind } } : e))
    },
    changeEdgeLabel(id: string, label: string) {
      setEdges(eds => eds.map(e => e.id === id ? { ...e, data: { ...e.data, label } } : e))
    },
    updateNodeBlocks(nodeId: string, blocks: Block[]) {
      setNodes(nds => nds.map(n => {
        if (n.id !== nodeId) return n
        const config = { ...(n.data.config as Record<string, unknown>), blocks }
        return { ...n, data: { ...n.data, config } }
      }))
    },
    updateNodeConfig(nodeId: string, partial: Partial<NodeConfig>) {
      setNodes(nds => nds.map(n => {
        if (n.id !== nodeId) return n
        const config = { ...(n.data.config as NodeConfig), ...partial }
        return { ...n, data: { ...n.data, config } }
      }))
    },
  }), [activeBoardId, setNodes, setEdges])

  // ── Custom onNodesChange: expand keyboard-delete to include descendants ──
  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    const removeSet = new Set(
      changes.filter(c => c.type === 'remove').map(c => (c as { id: string }).id),
    )
    if (removeSet.size === 0) {
      onNodesChange(changes)
      return
    }
    setNodes(currentNodes => {
      const toDelete = new Set<string>(removeSet)
      let changed = true
      while (changed) {
        changed = false
        for (const n of currentNodes) {
          if (n.parentId && toDelete.has(n.parentId) && !toDelete.has(n.id)) {
            toDelete.add(n.id)
            changed = true
          }
        }
      }
      const allChanges: NodeChange[] = [
        ...changes.filter(c => c.type !== 'remove'),
        ...[...toDelete].map(id => ({ type: 'remove' as const, id })),
      ]
      return applyNodeChanges(allChanges, currentNodes)
    })
  }, [onNodesChange, setNodes])

  // ── Drag-into-scope: assign/remove parentId on drop ──────────────────────
  const onNodeDragStop = useCallback((_event: MouseEvent | TouchEvent, draggedNode: Node) => {
    const allNodes = nodesRef.current

    const draggedAbs = draggedNode.parentId
      ? (() => {
          const p = computeAbsPos(draggedNode.parentId, allNodes)
          return { x: draggedNode.position.x + p.x, y: draggedNode.position.y + p.y }
        })()
      : { x: draggedNode.position.x, y: draggedNode.position.y }

    const nodeW = draggedNode.measured?.width ?? 180
    const nodeH = draggedNode.measured?.height ?? 50

    const desc = collectDescendants(draggedNode.id, allNodes)
    const candidateScopes = allNodes.filter(n => n.type === 'scope' && !desc.has(n.id))

    const containing = candidateScopes.filter(scope => {
      const sp = computeAbsPos(scope.id, allNodes)
      const sw = scope.measured?.width ?? (scope.style?.width as number) ?? 300
      const sh = scope.measured?.height ?? (scope.style?.height as number) ?? 200
      return (
        draggedAbs.x >= sp.x &&
        draggedAbs.y >= sp.y &&
        draggedAbs.x + nodeW <= sp.x + sw &&
        draggedAbs.y + nodeH <= sp.y + sh
      )
    })

    containing.sort((a, b) => {
      const aW = a.measured?.width ?? (a.style?.width as number) ?? 300
      const aH = a.measured?.height ?? (a.style?.height as number) ?? 200
      const bW = b.measured?.width ?? (b.style?.width as number) ?? 300
      const bH = b.measured?.height ?? (b.style?.height as number) ?? 200
      return (aW * aH) - (bW * bH)
    })

    const newParentId = containing[0]?.id
    if (newParentId === draggedNode.parentId) return

    setNodes(nds => {
      const updated = nds.map(n => {
        if (n.id !== draggedNode.id) return n
        if (newParentId) {
          const parentAbs = computeAbsPos(newParentId, nds)
          return {
            ...n,
            parentId: newParentId,
            position: { x: draggedAbs.x - parentAbs.x, y: draggedAbs.y - parentAbs.y },
          }
        }
        const { extent: _extent, parentId: _pid, ...rest } = n
        return { ...rest, position: draggedAbs }
      })
      return sortForRender(updated)
    })
  }, [setNodes])

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
        setSubject(data[0].meta ?? {})
        await loadBoardGraph(data[0].id)
        loadComments(data[0].id)
        loadSpec(data[0].id)
        loadFlowState(data[0].id)
      }
    } catch { /* backend may not be reachable */ }
  }

  async function loadBoardGraph(boardId: string) {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}`)
      if (!res.ok) return
      const data = await res.json()
      justLoadedRef.current = true
      setNodes(sortForRender(data.nodes ?? []))
      setEdges(data.edges ?? [])
    } catch { /* handled */ }
  }

  // ── Autosave on nodes/edges change ───────────────────────────────────────
  useEffect(() => {
    if (justLoadedRef.current) { justLoadedRef.current = false; return }
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

  // ── Subject persistence (1s debounce) ────────────────────────────────────
  function updateSubject(partial: Partial<BoardMeta>) {
    setSubject(prev => {
      const next = { ...prev, ...partial }
      if (!activeBoardIdRef.current) return next
      if (subjectTimerRef.current) clearTimeout(subjectTimerRef.current)
      subjectTimerRef.current = setTimeout(() => {
        fetch(`${API_BASE}/api/v1/boards/${activeBoardIdRef.current}/meta`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(next),
        }).catch(() => {})
      }, 1000)
      return next
    })
  }

  // ── Flow-state ────────────────────────────────────────────────────────────
  async function loadFlowState(boardId: string) {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}/flow-state`)
      if (res.ok) setFlowState(await res.json() as FlowState)
    } catch { /* offline */ }
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
      setSubject({})
      justLoadedRef.current = true
      setNodes([])
      setEdges([])
      setSaveStatus('saved')
      setFlowState(null)
      setGateComments([])
      setFrozenSpec(null)
    } catch { /* handled */ }
  }

  async function selectBoard(id: string) {
    if (id === activeBoardId) return
    setActiveBoardId(id)
    const board = boards.find(b => b.id === id)
    setSubject(board?.meta ?? {})
    await loadBoardGraph(id)
    setSaveStatus('saved')
    setRunLiveResult(null)
    setRunLiveError(null)
    setGateError(null)
    loadComments(id)
    loadSpec(id)
    loadFlowState(id)
  }

  // ── Gate: load comments ───────────────────────────────────────────────────
  async function loadComments(boardId: string) {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}/gate/comments`)
      if (!res.ok) return
      const data: GateComment[] = await res.json()
      setGateComments(data)
    } catch { /* backend offline */ }
  }

  // ── Gate: run AI check ────────────────────────────────────────────────────
  async function runGate() {
    if (!activeBoardId) return
    setGateRunning(true)
    setGateError(null)
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${activeBoardId}/gate/run`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) { setGateError(data.detail ?? `Error ${res.status}`); return }
      setGateComments(data)
      loadFlowState(activeBoardId)
    } catch (err) {
      setGateError(String(err))
    } finally {
      setGateRunning(false)
    }
  }

  // ── Gate: answer / dismiss ────────────────────────────────────────────────
  async function answerComment(commentId: string, answer: string): Promise<boolean> {
    if (!activeBoardId) return false
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/boards/${activeBoardId}/gate/comments/${commentId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer, status: 'answered' }),
        },
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        setGateError(`Failed to save answer: ${(detail as { detail?: string }).detail ?? res.status}`)
        return false
      }
      const updated: GateComment = await res.json()
      setGateComments(cs => cs.map(c => c.id === commentId ? updated : c))
      return true
    } catch (err) {
      setGateError(`Failed to save answer: ${err}`)
      return false
    }
  }

  async function dismissComment(commentId: string): Promise<boolean> {
    if (!activeBoardId) return false
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/boards/${activeBoardId}/gate/comments/${commentId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'rejected' }),
        },
      )
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}))
        setGateError(`Failed to dismiss: ${(detail as { detail?: string }).detail ?? res.status}`)
        return false
      }
      const updated: GateComment = await res.json()
      setGateComments(cs => cs.map(c => c.id === commentId ? updated : c))
      return true
    } catch (err) {
      setGateError(`Failed to dismiss: ${err}`)
      return false
    }
  }

  // ── Gate: load frozen spec ────────────────────────────────────────────────
  async function loadSpec(boardId: string) {
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}/spec`)
      if (res.ok) setFrozenSpec(await res.json() as FrozenSpec)
      else setFrozenSpec(null)  // 404 = not frozen; 500 = Supabase error — both mean no spec
    } catch { /* backend offline */ }
  }

  // ── Gate: verify answered comments ───────────────────────────────────────
  async function verifyAnswers() {
    if (!activeBoardId) return
    setVerifying(true)
    setGateError(null)
    try {
      const res = await fetch(`${API_BASE}/api/v1/boards/${activeBoardId}/gate/verify`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) { setGateError(data.detail ?? `Verify error ${res.status}`); return }
      const updated: GateComment[] = data
      setGateComments(cs => {
        const byId: Record<string, GateComment> = {}
        for (const c of updated) byId[c.id] = c
        return cs.map(c => byId[c.id] ?? c)
      })
      loadFlowState(activeBoardId)
    } catch (err) {
      setGateError(`Verify failed: ${err}`)
    } finally {
      setVerifying(false)
    }
  }

  // ── Run on inbox ──────────────────────────────────────────────────────────
  async function runLive(force = false) {
    if (!activeBoardId) return
    setRunLiveRunning(true)
    setRunLiveResult(null)
    setRunLiveError(null)
    try {
      const qs = force ? '?force=true' : ''
      const res = await fetch(`${API_BASE}/api/v1/boards/${activeBoardId}/run-live${qs}`, { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      if (!data.csv_content && !data.subject) {
        setRunLiveError('No email was processed — try again')
        return
      }
      setRunLiveResult(data)
    } catch (err: unknown) {
      setRunLiveError(err instanceof Error ? err.message : String(err))
    } finally {
      setRunLiveRunning(false)
    }
  }

  // ── Gate: pin-position math ───────────────────────────────────────────────
  // Converts a node's flow-space absolute position to canvas-area screen coords.
  function commentPinPos(nodeId: string, stackIndex: number) {
    const node = nodesRef.current.find(n => n.id === nodeId)
    if (!node) return null
    const abs = computeAbsPos(nodeId, nodesRef.current)
    const w = node.measured?.width ?? 180
    return {
      left: Math.round(abs.x * viewport.zoom + viewport.x + w * viewport.zoom + 2),
      top:  Math.round(abs.y * viewport.zoom + viewport.y + stackIndex * 24),
    }
  }

  // ── Add node from palette ─────────────────────────────────────────────────
  function addNode(kind: NodeKind, extraConfig: Partial<NodeConfig> = {}) {
    const isScope = kind === 'scope'
    const isAssumption = kind === 'assumption'
    const type = isScope ? 'scope' : isAssumption ? 'assumption' : 'process'
    const config: NodeConfig = { ...extraConfig }
    if (isScope && !config.scope_kind) config.scope_kind = 'for_each'

    const newNode: Node = {
      id: crypto.randomUUID(),
      type,
      position: { x: 120 + Math.random() * 280, y: 80 + Math.random() * 200 },
      data: { kind, title: NODE_LABELS[kind], config },
      ...(isScope && { style: { width: 400, height: 250 } }),
    }
    if (isScope) {
      setNodes(nds => sortForRender([newNode, ...nds]))
    } else {
      setNodes(nds => [...nds, newNode])
    }
  }

  // ── Open node editor modal on double-click ────────────────────────────────
  const onNodeDoubleClick = useCallback((_event: React.MouseEvent, node: Node) => {
    if (node.type !== 'scope') setEditingNodeId(node.id)
  }, [])

  const editingNode = editingNodeId ? nodes.find(n => n.id === editingNodeId) ?? null : null

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
  const saveLabel = saveStatus === 'saved' ? '✓ Saved' : saveStatus === 'saving' ? '… Saving' : '● Unsaved'
  const saveColor = saveStatus === 'saved' ? '#22c55e' : saveStatus === 'saving' ? '#9ca3af' : '#f59e0b'

  const inputBase: React.CSSProperties = {
    background: '#111827',
    border: '1px solid #374151',
    color: '#d1d5db',
    borderRadius: 4,
    padding: '3px 7px',
    fontSize: 12,
    outline: 'none',
  }

  return (
    <BoardContext.Provider value={boardContext}>
      <div style={{ width: '100%', height: '100vh', display: 'flex', flexDirection: 'column', background: '#030712', color: '#f9fafb', position: 'relative' }}>

        {/* ── Top bar ─────────────────────────────────────────────────── */}
        <div style={{
          height: 52, flexShrink: 0, borderBottom: '1px solid #1f2937',
          display: 'flex', alignItems: 'center', padding: '0 16px', gap: 0, background: '#030712',
        }}>

          {/* Wordmark */}
          <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.03em', color: '#f9fafb', flexShrink: 0 }}>
            Meridian
          </span>

          <div style={{ width: 1, height: 22, background: '#1f2937', margin: '0 14px', flexShrink: 0 }} />

          {/* Board control cluster */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, background: '#0b0f19', border: '1px solid #1f2937', borderRadius: 7, padding: '3px 4px 3px 6px', flexShrink: 0 }}>
            {boards.length > 0 ? (
              <select
                value={activeBoardId ?? ''}
                onChange={e => selectBoard(e.target.value)}
                style={{ background: 'transparent', border: 'none', color: '#f9fafb', padding: '3px 4px', fontSize: 13, outline: 'none', cursor: 'pointer', fontFamily: 'inherit' }}
              >
                {boards.map(b => <option key={b.id} value={b.id} style={{ background: '#0b0f19' }}>{b.name}</option>)}
              </select>
            ) : (
              <span style={{ fontSize: 13, color: '#4b5563', padding: '3px 4px' }}>No boards yet</span>
            )}
            <div style={{ width: 1, height: 16, background: '#374151', flexShrink: 0 }} />
            <button
              onClick={createBoard}
              style={{ background: 'none', border: 'none', color: '#6b7280', borderRadius: 5, padding: '3px 7px', fontSize: 12, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap' }}
              onMouseEnter={e => { e.currentTarget.style.color = '#f9fafb' }}
              onMouseLeave={e => { e.currentTarget.style.color = '#6b7280' }}
            >
              + New Board
            </button>
          </div>

          {/* Subject / keyed-by group — shown only when a board is active */}
          {activeBoardId && <>
            <div style={{ width: 1, height: 22, background: '#1f2937', margin: '0 14px', flexShrink: 0 }} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
              <span style={{ fontSize: 11, color: '#6b7280', whiteSpace: 'nowrap', fontWeight: 500 }}>Subject</span>
              <input
                value={subject.subject_name ?? ''}
                onChange={e => updateSubject({ subject_name: e.target.value })}
                placeholder="entity…"
                style={{ ...inputBase, width: 88 }}
              />
              <span style={{ fontSize: 11, color: '#6b7280', whiteSpace: 'nowrap', fontWeight: 500 }}>keyed by</span>
              <input
                value={subject.key_field ?? ''}
                onChange={e => updateSubject({ key_field: e.target.value })}
                placeholder="field…"
                style={{ ...inputBase, width: 80 }}
              />
            </div>
          </>}

          <div style={{ flex: 1 }} />

          {activeBoardId && (
            <span style={{ fontSize: 11, color: saveColor, fontWeight: 600 }}>{saveLabel}</span>
          )}
        </div>

        {/* ── Body ─────────────────────────────────────────────────────── */}
        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>

          {/* Palette */}
          <div className="palette-sidebar" style={{ width: 210, flexShrink: 0, borderRight: '1px solid #1f2937', padding: '10px 8px 20px', overflowY: 'auto', background: '#030712' }}>

            {/* ── Building blocks label ── */}
            <div style={{ fontSize: 10, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.1em', fontWeight: 700, padding: '2px 8px 6px' }}>
              Building blocks
            </div>

            {/* ── Custom — promoted top action ── */}
            <button
              onClick={() => addNode('custom')}
              title={NODE_DESCRIPTIONS['custom']}
              style={{
                width: '100%', display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 10px', marginBottom: 10,
                background: '#111827', border: '1px solid #374151',
                borderRadius: 7, color: '#d1d5db', fontSize: 12, fontWeight: 600,
                cursor: 'pointer', textAlign: 'left', boxSizing: 'border-box',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = '#1e293b'; e.currentTarget.style.borderColor = '#6b7280'; e.currentTarget.style.color = '#f9fafb' }}
              onMouseLeave={e => { e.currentTarget.style.background = '#111827'; e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#d1d5db' }}
            >
              <span style={{ fontSize: 14, width: 20, textAlign: 'center', flexShrink: 0 }}>{NODE_ICONS['custom']}</span>
              <span style={{ flex: 1 }}>+ Custom step</span>
            </button>

            {/* ── Grouped sections ── */}
            {PALETTE_GROUPS.map(group => (
              <div key={group.label} style={{ marginBottom: 4 }}>
                <div style={{ fontSize: 9, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 700, padding: '6px 8px 3px' }}>
                  {group.label}
                </div>
                {group.kinds.map(kind => (
                  <button
                    key={kind}
                    onClick={() => addNode(kind)}
                    title={NODE_DESCRIPTIONS[kind]}
                    style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', marginBottom: 2, background: 'none', border: '1px solid transparent', borderRadius: 6, color: '#9ca3af', fontSize: 12, fontWeight: 500, cursor: 'pointer', textAlign: 'left', boxSizing: 'border-box' }}
                    onMouseEnter={e => { e.currentTarget.style.background = '#111827'; e.currentTarget.style.borderColor = '#1f2937'; e.currentTarget.style.color = '#f9fafb' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.borderColor = 'transparent'; e.currentTarget.style.color = '#9ca3af' }}
                  >
                    <span style={{ fontSize: 13, width: 20, textAlign: 'center', flexShrink: 0 }}>{NODE_ICONS[kind]}</span>
                    {NODE_LABELS[kind]}
                  </button>
                ))}
              </div>
            ))}

            {/* ── Containers ── */}
            <div style={{ borderTop: '1px solid #1f2937', margin: '8px 4px 6px' }} />
            <div style={{ fontSize: 9, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 700, padding: '0 8px 5px' }}>
              Containers
            </div>
            <button
              onClick={() => addNode('scope')}
              title={NODE_DESCRIPTIONS['scope']}
              style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', marginBottom: 3, background: 'none', border: '1px dashed #1f2937', borderRadius: 6, color: '#818cf8', fontSize: 12, fontWeight: 500, cursor: 'pointer', textAlign: 'left', boxSizing: 'border-box' }}
              onMouseEnter={e => { e.currentTarget.style.background = '#0e0e2e'; e.currentTarget.style.borderColor = '#4f46e5' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.borderColor = '#1f2937' }}
            >
              <span style={{ fontSize: 13, width: 20, textAlign: 'center', flexShrink: 0 }}>↻</span>
              Do this for every
            </button>
            <button
              onClick={() => addNode('scope', { scope_kind: 'custom' })}
              title="A free-form group container — describe its purpose in your own words."
              style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', marginBottom: 3, background: 'none', border: '1px dashed #1f2937', borderRadius: 6, color: '#34d399', fontSize: 12, fontWeight: 500, cursor: 'pointer', textAlign: 'left', boxSizing: 'border-box' }}
              onMouseEnter={e => { e.currentTarget.style.background = '#061e16'; e.currentTarget.style.borderColor = '#065f46' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.borderColor = '#1f2937' }}
            >
              <span style={{ fontSize: 13, width: 20, textAlign: 'center', flexShrink: 0 }}>◻</span>
              Custom group
            </button>

            {/* ── Edge legend ── */}
            <div style={{ borderTop: '1px solid #1f2937', margin: '12px 4px 8px' }} />
            <div style={{ padding: '0 8px' }}>
              <div style={{ fontSize: 9, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 700, marginBottom: 8 }}>Edge types</div>
              {([
                ['default',   '#6b7280', '→ default',   false],
                ['on_pass',   '#22c55e', '✓ on_pass',   false],
                ['on_fail',   '#ef4444', '✗ on_fail',   false],
                ['exception', '#f59e0b', '⚠ exception', false],
                ['custom',    '#9ca3af', '✦ custom',    true],
              ] as const).map(([, color, label, dashed]) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5, fontSize: 11 }}>
                  <div style={{ width: 24, height: 0, flexShrink: 0, borderTop: dashed ? `2px dashed ${color}` : `2px solid ${color}` }} />
                  <span style={{ color: '#6b7280' }}>{label}</span>
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
          <div style={{ flex: 1, position: 'relative', minWidth: 0 }}>
            {!activeBoardId && (
              <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 10, pointerEvents: 'none' }}>
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
              onNodesChange={handleNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeDragStop={onNodeDragStop}
              onNodeDoubleClick={onNodeDoubleClick}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              colorMode="dark"
              fitView
              zoomOnDoubleClick={false}
              minZoom={0.1}
              maxZoom={2}
              deleteKeyCode={['Backspace', 'Delete']}
              style={{ background: '#060d1a' }}
              onMove={(_evt, vp) => setViewport(vp)}
            >
              <Background color="#1f2937" gap={20} size={1} />
              <Controls style={{ background: '#111827', border: '1px solid #1f2937' }} />
              <MiniMap
                nodeColor={n => (n.type === 'scope' ? '#4f46e5' : n.data?.kind ? '#4f46e5' : '#374151')}
                style={{ background: '#111827', border: '1px solid #1f2937' }}
              />
            </ReactFlow>

            {/* ── Gate comment pins — absolutely positioned over the canvas ── */}
            {activeBoardId && (() => {
              const byNode: Record<string, GateComment[]> = {}
              const boardLevel: GateComment[] = []
              for (const c of gateComments) {
                if (c.status === 'rejected') continue
                if (c.node_id) {
                  byNode[c.node_id] = [...(byNode[c.node_id] ?? []), c]
                } else {
                  boardLevel.push(c)
                }
              }
              return (
                <div style={{
                  position: 'absolute', inset: 0,
                  pointerEvents: 'none',
                  zIndex: 20,
                }}>
                  {/* Node-anchored pins */}
                  {Object.entries(byNode).flatMap(([nodeId, comments]) =>
                    comments.map((comment, idx) => {
                      const pos = commentPinPos(nodeId, idx)
                      if (!pos) return null
                      return (
                        <div
                          key={comment.id}
                          style={{
                            position: 'absolute',
                            left: pos.left,
                            top: pos.top,
                            pointerEvents: 'auto',
                            zIndex: 30,
                          }}
                        >
                          <GateBubble
                            comment={comment}
                            onAnswer={answerComment}
                            onDismiss={dismissComment}
                          />
                        </div>
                      )
                    })
                  )}

                  {/* Board-level comments — stacked in bottom-left corner */}
                  {boardLevel.length > 0 && (
                    <div style={{
                      position: 'absolute', bottom: 52, left: 12,
                      display: 'flex', flexDirection: 'column', gap: 4,
                      pointerEvents: 'auto', zIndex: 30,
                    }}>
                      <div style={{
                        fontSize: 9, color: '#4b5563', textTransform: 'uppercase',
                        fontWeight: 700, letterSpacing: '0.08em', paddingLeft: 2,
                      }}>
                        Board-level
                      </div>
                      {boardLevel.map(comment => (
                        <GateBubble
                          key={comment.id}
                          comment={comment}
                          onAnswer={answerComment}
                          onDismiss={dismissComment}
                        />
                      ))}
                    </div>
                  )}
                </div>
              )
            })()}
          </div>

          {/* ── Guided panel (right sidebar) ── */}
          <GuidedPanel
            boardId={activeBoardId}
            flowState={flowState}
            gateComments={gateComments}
            gateRunning={gateRunning}
            verifying={verifying}
            frozenSpec={frozenSpec}
            runLiveRunning={runLiveRunning}
            runLiveResult={runLiveResult}
            runLiveError={runLiveError}
            gateError={gateError}
            onRunGate={runGate}
            onVerifyAnswers={verifyAnswers}
            onOpenWorkedExample={() => setShowWorkedExampleModal(true)}
            onOpenBuildOrchestrator={() => setShowBuildOrchestrator(true)}
            onRunLive={runLive}
            onShowSpec={() => setSpecOpen(true)}
            onDismissRun={() => setRunLiveResult(null)}
          />
        </div>

        {/* ── Node editor modal ────────────────────────────────────── */}
        {editingNode && (
          <NodeEditorModal
            node={editingNode}
            onClose={() => setEditingNodeId(null)}
          />
        )}

        {/* ── Frozen spec viewer ───────────────────────────────────── */}
        {specOpen && frozenSpec && (
          <div
            onClick={() => setSpecOpen(false)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)',
              zIndex: 600, display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <div
              onClick={e => e.stopPropagation()}
              style={{
                width: 720, maxWidth: '95vw', maxHeight: '85vh',
                background: '#0d1117', border: '1px solid #22c55e44',
                borderRadius: 12, display: 'flex', flexDirection: 'column',
                overflow: 'hidden',
              }}
            >
              {/* Header */}
              <div style={{
                display: 'flex', alignItems: 'center', padding: '14px 18px',
                borderBottom: '1px solid #1f2937', flexShrink: 0,
              }}>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: '#f9fafb' }}>
                    Frozen Spec
                  </div>
                  <div style={{ fontSize: 11, color: '#4b5563', marginTop: 2 }}>
                    {frozenSpec.spec.board_name} · frozen {new Date(frozenSpec.frozen_at).toLocaleString()}
                    {' · '}{frozenSpec.spec.resolved_assumptions.length} assumption(s)
                  </div>
                </div>
                <div style={{ flex: 1 }} />
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(JSON.stringify(frozenSpec.spec, null, 2))
                  }}
                  style={{
                    background: 'none', border: '1px solid #374151',
                    borderRadius: 6, color: '#6b7280', fontSize: 12,
                    padding: '4px 10px', cursor: 'pointer', marginRight: 10,
                    fontFamily: 'inherit',
                  }}
                  onMouseEnter={e => { e.currentTarget.style.color = '#f9fafb'; e.currentTarget.style.borderColor = '#6b7280' }}
                  onMouseLeave={e => { e.currentTarget.style.color = '#6b7280'; e.currentTarget.style.borderColor = '#374151' }}
                >
                  Copy JSON
                </button>
                <button
                  onClick={() => setSpecOpen(false)}
                  style={{ background: 'none', border: 'none', color: '#4b5563', fontSize: 20, cursor: 'pointer', padding: 0, lineHeight: 1 }}
                  onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
                  onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
                >×</button>
              </div>

              {/* Resolved assumptions summary */}
              {frozenSpec.spec.resolved_assumptions.length > 0 && (
                <div style={{ padding: '12px 18px', borderBottom: '1px solid #1f2937', flexShrink: 0 }}>
                  <div style={{ fontSize: 10, color: '#4b5563', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 700, marginBottom: 8 }}>
                    Resolved assumptions ({frozenSpec.spec.resolved_assumptions.length})
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 180, overflowY: 'auto' }}>
                    {frozenSpec.spec.resolved_assumptions.map(a => (
                      <div key={a.comment_id} style={{ fontSize: 11, background: '#0a0f1a', borderRadius: 6, padding: '6px 10px', lineHeight: 1.5 }}>
                        <div style={{ color: a.severity === 'blocking' ? '#ef4444' : '#f59e0b', fontSize: 9, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 3 }}>
                          {a.severity} · {a.status}
                          {a.node_id && <span style={{ color: '#374151' }}> · node: {a.node_id.slice(0, 12)}…</span>}
                        </div>
                        <div style={{ color: '#e2e8f0' }}>Q: {a.question}</div>
                        {a.answer && <div style={{ color: '#4ade80', marginTop: 2 }}>A: {a.answer}</div>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Raw JSON */}
              <pre style={{
                flex: 1, overflow: 'auto', margin: 0,
                padding: '14px 18px', fontSize: 11, lineHeight: 1.6,
                color: '#6b7280', fontFamily: 'monospace', background: '#030712',
              }}>
                {JSON.stringify(frozenSpec.spec, null, 2)}
              </pre>
            </div>
          </div>
        )}

        {/* ── Worked example upload modal ── */}
        {showWorkedExampleModal && activeBoardId && (
          <WorkedExampleModal
            boardId={activeBoardId}
            onClose={() => setShowWorkedExampleModal(false)}
            onCaptured={() => {
              setShowWorkedExampleModal(false)
              loadFlowState(activeBoardId)
            }}
          />
        )}

        {/* ── Build orchestrator modal ── */}
        {showBuildOrchestrator && activeBoardId && (
          <BuildOrchestratorModal
            boardId={activeBoardId}
            onClose={() => setShowBuildOrchestrator(false)}
            onBuilt={() => {
              loadFlowState(activeBoardId)
              loadSpec(activeBoardId)
            }}
          />
        )}
      </div>
    </BoardContext.Provider>
  )
}
