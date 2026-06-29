import { useContext, useRef, useState } from 'react'
import { BoardContext } from '../context'
import {
  BLOCK_AVAILABILITY,
  BLOCK_LABELS,
  type Block,
  type BlockKind,
  type BranchConditionBlock,
  type CountKeyBlock,
  type DocFieldBlock,
  type MatchKeyBlock,
  type NodeKind,
  type NoteBlock,
  type RequiredFieldBlock,
  type SampleFileBlock,
} from '../types'

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

// ── Block factory ─────────────────────────────────────────────────────────

function createBlock(kind: BlockKind): Block {
  const id = crypto.randomUUID()
  switch (kind) {
    case 'required_field':
      return { id, kind, name: '', scope: 'document', required: true }
    case 'match_key':
      return { id, kind, source_collection: '', target_collection: '', key_field: '', on_missing: 'fail' }
    case 'count_key':
      return { id, kind, collection: '', dedup_key: '', label: '', track: [] }
    case 'branch_condition':
      return { id, kind, condition: '', outcome: '' }
    case 'sample_file':
      return { id, kind }
    case 'note':
      return { id, kind, text: '' }
    case 'doc_field':
      return { id, kind, name: '', appears_as: '', scope: 'document' }
  }
}

// ── Shared field style ────────────────────────────────────────────────────

const FIELD: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  borderBottom: '1px solid #1f2937',
  color: '#d1d5db',
  fontSize: 11,
  padding: '2px 3px',
  outline: 'none',
  fontFamily: 'inherit',
}

function stop(e: React.KeyboardEvent) { e.stopPropagation() }

// ── Block row ─────────────────────────────────────────────────────────────

export interface BlockRowProps {
  block: Block
  nodeId: string
  onUpdate: (changes: Record<string, unknown>) => void
  onDelete: () => void
  onMoveUp?: () => void
  onMoveDown?: () => void
}

export function BlockRow({ block, nodeId, onUpdate, onDelete, onMoveUp, onMoveDown }: BlockRowProps) {
  const board = useContext(BoardContext)!
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file || !board.activeBoardId) return
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('board_id', board.activeBoardId)
      fd.append('node_id', nodeId)
      const res = await fetch(`${API_BASE}/api/v1/files/upload`, { method: 'POST', body: fd })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const { file_id, filename, mime } = await res.json()
      onUpdate({ file_id, filename, mime })
    } catch (err) {
      console.error('Upload failed:', err)
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const kindLabel = BLOCK_LABELS[block.kind].replace('/ ', '')
  const showOrder = onMoveUp !== undefined || onMoveDown !== undefined

  return (
    <div style={{ padding: '6px 0', borderBottom: '1px solid #111827', display: 'flex', alignItems: 'flex-start', gap: 4 }}>

      {/* ↑↓ reorder handles */}
      {showOrder && (
        <div style={{ display: 'flex', flexDirection: 'column', paddingTop: 14, flexShrink: 0 }}>
          <button
            onClick={onMoveUp}
            disabled={!onMoveUp}
            onMouseDown={e => e.stopPropagation()}
            title="Move up"
            style={{
              background: 'none', border: 'none', padding: '1px 4px', lineHeight: 1,
              fontSize: 9, cursor: onMoveUp ? 'pointer' : 'default',
              color: onMoveUp ? '#4b5563' : '#1f2937',
            }}
            onMouseEnter={e => { if (onMoveUp) e.currentTarget.style.color = '#9ca3af' }}
            onMouseLeave={e => { e.currentTarget.style.color = onMoveUp ? '#4b5563' : '#1f2937' }}
          >▲</button>
          <button
            onClick={onMoveDown}
            disabled={!onMoveDown}
            onMouseDown={e => e.stopPropagation()}
            title="Move down"
            style={{
              background: 'none', border: 'none', padding: '1px 4px', lineHeight: 1,
              fontSize: 9, cursor: onMoveDown ? 'pointer' : 'default',
              color: onMoveDown ? '#4b5563' : '#1f2937',
            }}
            onMouseEnter={e => { if (onMoveDown) e.currentTarget.style.color = '#9ca3af' }}
            onMouseLeave={e => { e.currentTarget.style.color = onMoveDown ? '#4b5563' : '#1f2937' }}
          >▼</button>
        </div>
      )}

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 9, color: '#374151', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 700, marginBottom: 4 }}>
          {kindLabel}
        </div>

        {/* ── required_field ── */}
        {block.kind === 'required_field' && (() => {
          const b = block as RequiredFieldBlock
          return (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
              <input style={{ ...FIELD, flex: 1, minWidth: 60 }} value={b.name} onChange={e => onUpdate({ name: e.target.value })} onKeyDown={stop} placeholder="field name" />
              <select value={b.scope} onChange={e => onUpdate({ scope: e.target.value })} style={{ background: '#0f172a', border: '1px solid #1f2937', color: '#9ca3af', borderRadius: 3, fontSize: 10, padding: '1px 3px' }}>
                <option value="document">document</option>
                <option value="line_item">line item</option>
              </select>
              <label style={{ fontSize: 10, color: '#6b7280', display: 'flex', alignItems: 'center', gap: 3, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                <input type="checkbox" checked={b.required} onChange={e => onUpdate({ required: e.target.checked })} style={{ width: 10, height: 10 }} />
                req&apos;d
              </label>
            </div>
          )
        })()}

        {/* ── match_key ── */}
        {block.kind === 'match_key' && (() => {
          const b = block as MatchKeyBlock
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <div style={{ display: 'flex', gap: 4 }}>
                <input style={{ ...FIELD, flex: 1 }} value={b.source_collection ?? ''} onChange={e => onUpdate({ source_collection: e.target.value })} onKeyDown={stop} placeholder="source collection" />
                <input style={{ ...FIELD, flex: 1 }} value={b.target_collection ?? ''} onChange={e => onUpdate({ target_collection: e.target.value })} onKeyDown={stop} placeholder="target collection" />
              </div>
              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                <input style={{ ...FIELD, flex: 1 }} value={b.key_field ?? ''} onChange={e => onUpdate({ key_field: e.target.value })} onKeyDown={stop} placeholder="key field" />
                <select value={b.on_missing ?? 'fail'} onChange={e => onUpdate({ on_missing: e.target.value })} style={{ background: '#0f172a', border: '1px solid #1f2937', color: '#9ca3af', borderRadius: 3, fontSize: 10, padding: '1px 3px', flexShrink: 0 }}>
                  <option value="fail">fail</option>
                  <option value="flag">flag</option>
                  <option value="ignore">ignore</option>
                </select>
              </div>
            </div>
          )
        })()}

        {/* ── count_key ── */}
        {block.kind === 'count_key' && (() => {
          const b = block as CountKeyBlock
          const track: string[] = b.track ?? []
          const toggleTrack = (v: string) => {
            const next = track.includes(v) ? track.filter(x => x !== v) : [...track, v]
            onUpdate({ track: next })
          }
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <input style={{ ...FIELD, width: '100%', boxSizing: 'border-box' }} value={b.collection ?? ''} onChange={e => onUpdate({ collection: e.target.value })} onKeyDown={stop} placeholder="collection" />
              <div style={{ display: 'flex', gap: 4 }}>
                <input style={{ ...FIELD, flex: 1 }} value={b.dedup_key ?? ''} onChange={e => onUpdate({ dedup_key: e.target.value })} onKeyDown={stop} placeholder="dedup key" />
                <input style={{ ...FIELD, flex: 1 }} value={b.label ?? ''} onChange={e => onUpdate({ label: e.target.value })} onKeyDown={stop} placeholder="label" />
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
                {(['processed', 'succeeded', 'failed'] as const).map(v => (
                  <label key={v} style={{ fontSize: 10, color: track.includes(v) ? '#d1d5db' : '#4b5563', display: 'flex', alignItems: 'center', gap: 2, cursor: 'pointer' }}>
                    <input type="checkbox" checked={track.includes(v)} onChange={() => toggleTrack(v)} style={{ width: 10, height: 10 }} />
                    {v}
                  </label>
                ))}
              </div>
            </div>
          )
        })()}

        {/* ── branch_condition ── */}
        {block.kind === 'branch_condition' && (() => {
          const b = block as BranchConditionBlock
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <input style={{ ...FIELD, width: '100%', boxSizing: 'border-box' }} value={b.condition} onChange={e => onUpdate({ condition: e.target.value })} onKeyDown={stop} placeholder="condition" />
              <input style={{ ...FIELD, width: '100%', boxSizing: 'border-box' }} value={b.outcome} onChange={e => onUpdate({ outcome: e.target.value })} onKeyDown={stop} placeholder="outcome" />
            </div>
          )
        })()}

        {/* ── note ── */}
        {block.kind === 'note' && (() => {
          const b = block as NoteBlock
          return (
            <textarea value={b.text} onChange={e => onUpdate({ text: e.target.value })} onKeyDown={stop} rows={2} placeholder="note…" className="nodrag nopan" style={{ ...FIELD, width: '100%', resize: 'none', boxSizing: 'border-box' }} />
          )
        })()}

        {/* ── doc_field ── */}
        {block.kind === 'doc_field' && (() => {
          const b = block as DocFieldBlock
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                <input
                  style={{ ...FIELD, flex: 1 }}
                  value={b.name}
                  onChange={e => onUpdate({ name: e.target.value })}
                  onKeyDown={stop}
                  placeholder="field name, e.g. Batch No"
                />
                <select
                  value={b.scope ?? 'document'}
                  onChange={e => onUpdate({ scope: e.target.value })}
                  style={{ background: '#0f172a', border: '1px solid #1f2937', color: '#9ca3af', borderRadius: 3, fontSize: 10, padding: '1px 3px', flexShrink: 0 }}
                >
                  <option value="document">document</option>
                  <option value="line_item">line item</option>
                </select>
              </div>
              <input
                style={{ ...FIELD, width: '100%', boxSizing: 'border-box', color: '#6b7280', fontSize: 10 }}
                value={b.appears_as ?? ''}
                onChange={e => onUpdate({ appears_as: e.target.value })}
                onKeyDown={stop}
                placeholder="how to recognise it on the doc (optional)"
              />
            </div>
          )
        })()}

        {/* ── sample_file ── */}
        {block.kind === 'sample_file' && (() => {
          const b = block as SampleFileBlock
          if (b.file_id) {
            return (
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 12 }}>📎</span>
                <span style={{ fontSize: 11, color: '#4ade80', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{b.filename}</span>
              </div>
            )
          }
          if (uploading) return <span style={{ fontSize: 11, color: '#60a5fa' }}>Uploading…</span>
          return (
            <>
              <input ref={fileRef} type="file" accept=".pdf,.txt,.csv" style={{ display: 'none' }} onChange={handleFile} />
              <button
                onClick={() => fileRef.current?.click()}
                onMouseDown={e => e.stopPropagation()}
                style={{ background: 'none', border: '1px dashed #374151', color: '#6b7280', borderRadius: 4, padding: '3px 8px', fontSize: 11, cursor: 'pointer', width: '100%', boxSizing: 'border-box' }}
                onMouseEnter={e => { e.currentTarget.style.color = '#9ca3af'; e.currentTarget.style.borderColor = '#6b7280' }}
                onMouseLeave={e => { e.currentTarget.style.color = '#6b7280'; e.currentTarget.style.borderColor = '#374151' }}
              >
                ↑ Upload PDF / TXT / CSV
              </button>
            </>
          )
        })()}
      </div>

      <button
        onClick={onDelete}
        onMouseDown={e => e.stopPropagation()}
        style={{ background: 'none', border: 'none', color: '#374151', cursor: 'pointer', fontSize: 14, padding: 0, lineHeight: 1, flexShrink: 0, marginTop: 14 }}
        onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
        onMouseLeave={e => (e.currentTarget.style.color = '#374151')}
      >
        ×
      </button>
    </div>
  )
}

// ── Main BlockEditor ───────────────────────────────────────────────────────

export interface BlockEditorProps {
  nodeId: string
  nodeKind: NodeKind
  blocks: Block[]
  showAddButtons?: boolean
  showReorder?: boolean
}

export function BlockEditor({ nodeId, nodeKind, blocks, showAddButtons, showReorder }: BlockEditorProps) {
  const board = useContext(BoardContext)!
  const [slashText, setSlashText] = useState('')
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const available = BLOCK_AVAILABILITY[nodeKind]
  const query = slashText.startsWith('/') ? slashText.slice(1).toLowerCase().trim() : ''
  const filtered = available.filter(k =>
    !query ||
    k.replace(/_/g, ' ').includes(query) ||
    BLOCK_LABELS[k].toLowerCase().includes(query),
  )

  function setBlocks(next: Block[]) { board.updateNodeBlocks(nodeId, next) }

  function addBlock(kind: BlockKind) {
    setBlocks([...blocks, createBlock(kind)])
    setSlashText('')
    setPaletteOpen(false)
    setActiveIdx(0)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  function deleteBlock(blockId: string) { setBlocks(blocks.filter(b => b.id !== blockId)) }

  function updateBlock(blockId: string, changes: Record<string, unknown>) {
    setBlocks(blocks.map(b => b.id === blockId ? { ...b, ...changes } as Block : b))
  }

  function moveBlock(idx: number, direction: -1 | 1) {
    const target = idx + direction
    if (target < 0 || target >= blocks.length) return
    const next = [...blocks]
    ;[next[idx], next[target]] = [next[target], next[idx]]
    setBlocks(next)
  }

  function onInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const v = e.target.value
    setSlashText(v)
    setPaletteOpen(v.startsWith('/'))
    setActiveIdx(0)
  }

  function onInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    e.stopPropagation()
    if (!paletteOpen || filtered.length === 0) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setActiveIdx(i => Math.min(i + 1, filtered.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActiveIdx(i => Math.max(i - 1, 0)) }
    else if (e.key === 'Enter')  { e.preventDefault(); addBlock(filtered[activeIdx]) }
    else if (e.key === 'Escape') { setPaletteOpen(false); setSlashText('') }
  }

  function onInputBlur() {
    setTimeout(() => { setPaletteOpen(false); setSlashText('') }, 200)
  }

  if (available.length === 0) return null

  return (
    <div className="nodrag nopan" style={{ marginTop: 8 }}>

      {/* Quick-add buttons */}
      {showAddButtons && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginBottom: 10 }}>
          {available.map(kind => (
            <button
              key={kind}
              onClick={() => addBlock(kind)}
              onMouseDown={e => e.stopPropagation()}
              style={{
                background: 'none',
                border: '1px solid #374151',
                borderRadius: 5,
                color: '#6b7280',
                fontSize: 11,
                padding: '4px 10px',
                cursor: 'pointer',
                fontFamily: 'inherit',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = '#1e293b'; e.currentTarget.style.color = '#d1d5db'; e.currentTarget.style.borderColor = '#6b7280' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.color = '#6b7280'; e.currentTarget.style.borderColor = '#374151' }}
            >
              + {BLOCK_LABELS[kind].replace('/ ', '')}
            </button>
          ))}
        </div>
      )}

      {/* Block list */}
      <div>
        {blocks.map((block, idx) => (
          <BlockRow
            key={block.id}
            block={block}
            nodeId={nodeId}
            onUpdate={changes => updateBlock(block.id, changes)}
            onDelete={() => deleteBlock(block.id)}
            onMoveUp={showReorder && idx > 0 ? () => moveBlock(idx, -1) : undefined}
            onMoveDown={showReorder && idx < blocks.length - 1 ? () => moveBlock(idx, 1) : undefined}
          />
        ))}
      </div>

      {/* "/" palette input */}
      <div style={{ position: 'relative', marginTop: blocks.length > 0 ? 8 : 0 }}>
        {paletteOpen && filtered.length > 0 && (
          <div style={{
            position: 'absolute', bottom: '100%', left: 0, right: 0,
            background: '#0f172a', border: '1px solid #374151',
            borderRadius: 6, padding: 3, marginBottom: 2,
            zIndex: 200, boxShadow: '0 4px 20px rgba(0,0,0,0.6)',
          }}>
            {filtered.map((kind, i) => (
              <div
                key={kind}
                onMouseDown={e => { e.preventDefault(); addBlock(kind) }}
                onMouseEnter={() => setActiveIdx(i)}
                style={{
                  padding: '5px 8px', borderRadius: 4, cursor: 'pointer',
                  background: i === activeIdx ? '#1e293b' : 'transparent',
                  color: i === activeIdx ? '#f1f5f9' : '#9ca3af',
                  fontSize: 12, fontWeight: i === activeIdx ? 500 : 400,
                }}
              >
                {BLOCK_LABELS[kind]}
              </div>
            ))}
          </div>
        )}
        <input
          ref={inputRef}
          value={slashText}
          onChange={onInputChange}
          onKeyDown={onInputKeyDown}
          onBlur={onInputBlur}
          placeholder='Type "/" to add a block…'
          className="nodrag nopan"
          style={{
            width: '100%', background: 'transparent', border: 'none',
            borderBottom: '1px solid #1f2937', color: '#4b5563', fontSize: 11,
            padding: '4px 0', outline: 'none', boxSizing: 'border-box', fontFamily: 'inherit',
          }}
        />
      </div>
    </div>
  )
}
