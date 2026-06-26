import { useState } from 'react'

type LegStatus = 'idle' | 'pending' | 'ok' | 'not_configured' | 'not_run' | 'error'

interface LegResult {
  status: LegStatus
  detail?: string | Record<string, string>
  workflow_id?: string
}

interface SkeletonResult {
  temporal: LegResult
  composio: LegResult
  supabase: LegResult
}

function icon(status: LegStatus): string {
  if (status === 'ok') return '✓'
  if (status === 'pending') return '⋯'
  if (status === 'not_configured') return '⚙'
  if (status === 'error' || status === 'not_run') return '✗'
  return '—'
}

function borderColor(status: LegStatus): string {
  if (status === 'ok') return '#22c55e'
  if (status === 'not_configured') return '#f59e0b'
  if (status === 'error' || status === 'not_run') return '#ef4444'
  if (status === 'pending') return '#60a5fa'
  return '#374151'
}

interface CardProps {
  label: string
  status: LegStatus
  detail?: string | null
}

function StatusCard({ label, status, detail }: CardProps) {
  const color = borderColor(status)
  return (
    <div style={{
      border: `2px solid ${color}`,
      borderRadius: 8,
      padding: '16px 20px',
      minWidth: 220,
      maxWidth: 300,
      background: '#111827',
      fontFamily: 'monospace',
    }}>
      <div style={{ fontSize: 24, marginBottom: 4 }}>{icon(status)}</div>
      <div style={{ fontWeight: 700, fontSize: 14, color: '#f9fafb', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 12, color, textTransform: 'uppercase', marginBottom: 8 }}>{status}</div>
      {detail && (
        <pre style={{ fontSize: 11, color: '#9ca3af', margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
          {detail}
        </pre>
      )}
    </div>
  )
}

export default function SkeletonPage() {
  const [loading, setLoading] = useState(false)
  const [apiStatus, setApiStatus] = useState<LegStatus>('idle')
  const [result, setResult] = useState<SkeletonResult | null>(null)
  const [fetchError, setFetchError] = useState<string | null>(null)

  async function runSkeleton() {
    setLoading(true)
    setResult(null)
    setFetchError(null)
    setApiStatus('pending')

    const base = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'
    try {
      const res = await fetch(`${base}/api/v1/skeleton/run`, { method: 'POST' })
      if (res.ok) {
        setApiStatus('ok')
        setResult(await res.json())
      } else {
        setApiStatus('error')
        setFetchError(`HTTP ${res.status}`)
      }
    } catch (err) {
      setApiStatus('error')
      setFetchError(String(err))
    } finally {
      setLoading(false)
    }
  }

  function legDetail(leg: LegResult | undefined): string | null {
    if (!leg) return null
    const d = leg.detail ?? leg.workflow_id
    if (!d) return null
    return typeof d === 'string' ? d : JSON.stringify(d, null, 2)
  }

  const temporalStatus: LegStatus = loading ? 'pending' : (result?.temporal?.status ?? 'idle')
  const composioStatus: LegStatus = loading ? 'pending' : (result?.composio?.status ?? 'idle')
  const supabaseStatus: LegStatus = loading ? 'pending' : (result?.supabase?.status ?? 'idle')

  return (
    <div className="shell">
      {/* Nav */}
      <div style={{ position: 'absolute', top: 20, right: 24, display: 'flex', gap: 8 }}>
        <a
          href="#/"
          style={{
            fontSize: 13,
            color: '#6b7280',
            textDecoration: 'none',
            padding: '5px 12px',
            borderRadius: 6,
            border: '1px solid #374151',
            background: '#111827',
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
          onMouseLeave={e => (e.currentTarget.style.color = '#6b7280')}
        >
          ← Canvas
        </a>
        <span style={{ fontSize: 13, color: '#f9fafb', padding: '5px 12px', borderRadius: 6, border: '1px solid #4f46e5', background: '#1e1b4b' }}>
          Skeleton
        </span>
      </div>

      <div className="eyebrow">Walking Skeleton</div>
      <h1>Stack Verification</h1>
      <p className="subtitle">
        Proves the full click path: React → FastAPI → Temporal workflow → activity (Composio + Supabase) → back.
      </p>

      <button className="run-btn" onClick={runSkeleton} disabled={loading}>
        {loading ? 'Running…' : 'Run walking skeleton'}
      </button>

      <div className="cards">
        <StatusCard
          label="API (FastAPI)"
          status={loading ? 'pending' : apiStatus}
          detail={fetchError ?? (apiStatus === 'ok' ? 'HTTP 200' : null)}
        />
        <StatusCard label="Temporal" status={temporalStatus} detail={legDetail(result?.temporal)} />
        <StatusCard label="Composio" status={composioStatus} detail={legDetail(result?.composio)} />
        <StatusCard label="Supabase" status={supabaseStatus} detail={legDetail(result?.supabase)} />
      </div>
    </div>
  )
}
