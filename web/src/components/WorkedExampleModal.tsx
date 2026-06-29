import { useRef, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

const FIELD_LABELS: Record<string, string> = {
  shipment_number:    'Shipment number',
  invoices_processed: 'Invoices found',
  invoices_succeeded: 'Invoices passed',
  invoices_failed:    'Invoices failed',
  goods_failed:       'Products with issues',
  batches_processed:  'Batches checked',
  batches_succeeded:  'Certificates matched',
  batches_failed:     'Certificates not matched',
}

interface UploadResult {
  captured: boolean
  answer_key: Record<string, number | string | null>
  needs_confirmation: boolean
  files_uploaded: number
}

interface Props {
  boardId: string
  onClose(): void
  onCaptured(): void
}

interface ZoneProps {
  label: string
  hint: string
  accept: string
  multiple?: boolean
  displayNames: string[]
  onFiles(files: File[]): void
}

function DropZone({ label, hint, accept, multiple = false, displayNames, onFiles }: ZoneProps) {
  const [hovered, setHovered] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const hasFiles = displayNames.length > 0

  return (
    <div
      onDragOver={e => { e.preventDefault(); setHovered(true) }}
      onDragLeave={() => setHovered(false)}
      onDrop={e => { e.preventDefault(); setHovered(false); onFiles(Array.from(e.dataTransfer.files)) }}
      style={{
        border: `2px dashed ${hovered || hasFiles ? '#4f46e5' : '#374151'}`,
        borderRadius: 8, padding: '12px 14px',
        background: hovered ? 'rgba(79,70,229,0.07)' : hasFiles ? 'rgba(79,70,229,0.03)' : 'transparent',
        transition: 'border-color 0.12s, background 0.12s',
      }}
    >
      <input
        ref={inputRef} type="file" accept={accept} multiple={multiple} style={{ display: 'none' }}
        onChange={e => { onFiles(Array.from(e.target.files ?? [])); e.target.value = '' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#f9fafb', marginBottom: 2 }}>{label}</div>
          {hasFiles ? (
            <div style={{ fontSize: 11, color: '#4ade80', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {displayNames.join(', ')}
            </div>
          ) : (
            <div style={{ fontSize: 11, color: '#4b5563' }}>{hint}</div>
          )}
        </div>
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          style={{
            background: 'none', border: '1px solid #374151', borderRadius: 5,
            padding: '4px 10px', fontSize: 11, color: '#9ca3af', cursor: 'pointer',
            whiteSpace: 'nowrap', fontFamily: 'inherit', flexShrink: 0,
          }}
          onMouseEnter={e => { e.currentTarget.style.borderColor = '#6b7280'; e.currentTarget.style.color = '#f9fafb' }}
          onMouseLeave={e => { e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#9ca3af' }}
        >
          {hasFiles ? 'Change' : 'Browse'}
        </button>
      </div>
    </div>
  )
}

export function WorkedExampleModal({ boardId, onClose, onCaptured }: Props) {
  const [emailFile, setEmailFile]           = useState<File | null>(null)
  const [attachFiles, setAttachFiles]       = useState<File[]>([])
  const [expectedFile, setExpectedFile]     = useState<File | null>(null)
  const [fixtureSubject, setFixtureSubject] = useState('')
  const [uploading, setUploading]           = useState(false)
  const [error, setError]                   = useState<string | null>(null)
  const [result, setResult]                 = useState<UploadResult | null>(null)

  const canUpload = !!emailFile && !!expectedFile

  async function handleUpload() {
    if (!canUpload) return
    setUploading(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('email', emailFile)
      attachFiles.forEach(f => form.append('attachments', f))
      form.append('expected_output', expectedFile)
      if (fixtureSubject.trim()) form.append('fixture_subject', fixtureSubject.trim())
      const res = await fetch(`${API_BASE}/api/v1/boards/${boardId}/worked-example`, {
        method: 'POST', body: form,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setResult(data as UploadResult)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setUploading(false)
    }
  }

  const overlay: React.CSSProperties = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.8)',
    zIndex: 500, display: 'flex', alignItems: 'center', justifyContent: 'center',
  }

  const modal: React.CSSProperties = {
    width: 520, maxWidth: '95vw', maxHeight: '90vh',
    background: '#0d1117', border: '1px solid #1f2937',
    borderRadius: 12, display: 'flex', flexDirection: 'column', overflow: 'hidden',
  }

  return (
    <div onClick={onClose} style={overlay}>
      <div onClick={e => e.stopPropagation()} style={modal}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid #1f2937', flexShrink: 0 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#f9fafb' }}>Upload a worked example</div>
            <div style={{ fontSize: 12, color: '#4b5563', marginTop: 2 }}>
              Show your agent what a good result looks like
            </div>
          </div>
          <div style={{ flex: 1 }} />
          <button
            onClick={onClose}
            style={{ background: 'none', border: 'none', color: '#4b5563', fontSize: 22, cursor: 'pointer', padding: 0, lineHeight: 1 }}
            onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
            onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
          >×</button>
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '20px' }}>
          {!result ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {/* Email */}
              <DropZone
                label="The email"
                hint="The email body or .eml file that triggered the process"
                accept=".txt,.eml,.pdf,.msg"
                displayNames={emailFile ? [emailFile.name] : []}
                onFiles={fs => { if (fs[0]) setEmailFile(fs[0]) }}
              />

              {/* Attachments */}
              <DropZone
                label="Attachments"
                hint="Invoices, certificates, or any files that came with the email"
                accept=".pdf,.xlsx,.xls,.csv,.docx,.doc,.txt,.png,.jpg"
                multiple
                displayNames={attachFiles.map(f => f.name)}
                onFiles={fs => setAttachFiles(prev => [...prev, ...fs])}
              />
              {attachFiles.length > 0 && (
                <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: -8 }}>
                  <button
                    onClick={() => setAttachFiles([])}
                    style={{ background: 'none', border: 'none', color: '#4b5563', fontSize: 11, cursor: 'pointer', fontFamily: 'inherit' }}
                    onMouseEnter={e => (e.currentTarget.style.color = '#f87171')}
                    onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
                  >
                    Clear all attachments
                  </button>
                </div>
              )}

              {/* Expected output */}
              <DropZone
                label="Expected result"
                hint="A CSV or spreadsheet showing what your agent should output"
                accept=".csv,.xlsx,.xls,.pdf,.txt"
                displayNames={expectedFile ? [expectedFile.name] : []}
                onFiles={fs => { if (fs[0]) setExpectedFile(fs[0]) }}
              />

              {/* Optional subject */}
              <div>
                <label style={{ fontSize: 12, color: '#9ca3af', display: 'block', marginBottom: 5 }}>
                  Shipment / subject key <span style={{ color: '#4b5563' }}>(optional)</span>
                </label>
                <input
                  value={fixtureSubject}
                  onChange={e => setFixtureSubject(e.target.value)}
                  placeholder="e.g. MAWB number or shipment ID…"
                  style={{
                    width: '100%', boxSizing: 'border-box',
                    background: '#111827', border: '1px solid #374151',
                    color: '#d1d5db', borderRadius: 6, padding: '8px 10px', fontSize: 12, outline: 'none',
                  }}
                />
              </div>

              {error && (
                <div style={{ fontSize: 12, color: '#f87171', background: '#1c0a0a', borderRadius: 6, padding: '8px 12px' }}>
                  ✗ {error}
                </div>
              )}

              {/* Upload button */}
              <button
                onClick={handleUpload}
                disabled={!canUpload || uploading}
                style={{
                  padding: '10px', borderRadius: 7, fontSize: 13, fontWeight: 700,
                  background: !canUpload || uploading ? '#1e293b' : '#312e81',
                  border: `1px solid ${!canUpload || uploading ? '#374151' : '#4f46e5'}`,
                  color: !canUpload || uploading ? '#4b5563' : '#a5b4fc',
                  cursor: !canUpload || uploading ? 'default' : 'pointer', fontFamily: 'inherit',
                }}
                onMouseEnter={e => { if (canUpload && !uploading) { e.currentTarget.style.background = '#3730a3'; e.currentTarget.style.color = '#f9fafb' } }}
                onMouseLeave={e => { if (canUpload && !uploading) { e.currentTarget.style.background = '#312e81'; e.currentTarget.style.color = '#a5b4fc' } }}
              >
                {uploading ? '⏳ Uploading…' : 'Upload example'}
              </button>

              {!canUpload && (
                <div style={{ fontSize: 11, color: '#4b5563', textAlign: 'center' }}>
                  Add an email file and expected result to continue
                </div>
              )}
            </div>
          ) : (
            /* Success state */
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <div style={{ width: 32, height: 32, borderRadius: '50%', background: '#14532d', border: '1px solid #166534', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#4ade80', fontSize: 16, flexShrink: 0 }}>✓</div>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: '#f9fafb' }}>Example uploaded</div>
                  <div style={{ fontSize: 11, color: '#4b5563', marginTop: 1 }}>{result.files_uploaded} file{result.files_uploaded !== 1 ? 's' : ''} saved</div>
                </div>
              </div>

              {result.needs_confirmation && (
                <div style={{ background: '#1c1200', border: '1px solid #ca8a0444', borderRadius: 7, padding: '10px 12px', fontSize: 12, color: '#fbbf24', lineHeight: 1.5 }}>
                  We couldn't fully read your expected results file. Your agent can still be built — the numbers just won't be auto-checked. You can upload a cleaner CSV later.
                </div>
              )}

              {/* Parsed answer key */}
              <div style={{ background: '#0a0f1a', borderRadius: 7, padding: '12px 14px' }}>
                <div style={{ fontSize: 11, color: '#4b5563', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>
                  What we expect your agent to produce
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                  {Object.entries(FIELD_LABELS).map(([field, label]) => {
                    const val = result.answer_key[field]
                    return (
                      <div key={field} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                        <span style={{ fontSize: 12, color: '#6b7280' }}>{label}</span>
                        <span style={{ fontSize: 13, fontWeight: 600, color: val != null ? '#f9fafb' : '#374151' }}>
                          {val != null ? String(val) : '—'}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>

              <button
                onClick={() => { onCaptured(); onClose() }}
                style={{
                  padding: '10px', borderRadius: 7, fontSize: 13, fontWeight: 700,
                  background: '#14532d', border: '1px solid #16a34a', color: '#4ade80',
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
                onMouseEnter={e => { e.currentTarget.style.background = '#166534'; e.currentTarget.style.color = '#86efac' }}
                onMouseLeave={e => { e.currentTarget.style.background = '#14532d'; e.currentTarget.style.color = '#4ade80' }}
              >
                Continue to next step →
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
