import { useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

const FIELD_PLAIN: Record<string, string> = {
  shipment_number:    'shipment number',
  invoices_processed: 'invoices found',
  invoices_succeeded: 'invoices that passed',
  invoices_failed:    'invoices that failed',
  goods_failed:       'products with issues',
  batches_processed:  'batches checked',
  batches_succeeded:  'certificates matched',
  batches_failed:     'certificates not matched',
}

type Phase = 'idle' | 'freezing' | 'generating' | 'evaluating' | 'healing' | 'done' | 'failed'

interface FieldResult {
  field: string
  expected: number | string | null
  actual: number | string | null
  passed: boolean | null
  note?: string
}

interface EvalResult {
  passed: boolean
  summary?: string
  answer_key?: {
    passed?: boolean | null
    field_results?: FieldResult[]
    pass_count?: number
  }
}

interface HealHistory {
  attempt: number
  eval_passed: boolean
  pass_count: number
  total_checks: number
  failed_checks?: string[]
}

interface HealResult {
  status: string
  attempts: number
  history?: HealHistory[]
  final_eval?: EvalResult
}

interface Props {
  boardId: string
  onClose(): void
  onBuilt(): void
}

const PHASE_LABEL: Record<Exclude<Phase, 'idle' | 'done' | 'failed'>, string> = {
  freezing:   'Locking in your process…',
  generating: 'Writing your agent…',
  evaluating: 'Testing it on your example…',
  healing:    'Fixing the issues…',
}

function Spinner() {
  return (
    <span style={{
      display: 'inline-block', width: 14, height: 14, borderRadius: '50%',
      border: '2px solid #374151', borderTopColor: '#a5b4fc',
      animation: 'spin 0.8s linear infinite', flexShrink: 0,
    }} />
  )
}

const HEAL_STATUS_LABELS: Record<string, string> = {
  healed:             'Fixed — all checks passing',
  max_attempts:       'Reached max attempts — some checks may still be off',
  stalled:            'No more improvements found — your agent is as good as it can get',
  revert_validation:  'Code validation failed — reverted to last working version',
  revert_regression:  'A fix made things worse — reverted to last working version',
  agent_error:        'Something went wrong — check the logs',
}

export function BuildOrchestratorModal({ boardId, onClose, onBuilt }: Props) {
  const [phase, setPhase]         = useState<Phase>('idle')
  const [evalResult, setEvalResult] = useState<EvalResult | null>(null)
  const [healResult, setHealResult] = useState<HealResult | null>(null)
  const [error, setError]         = useState<string | null>(null)

  const failedFields = evalResult?.answer_key?.field_results?.filter(r => r.passed === false) ?? []
  const finalEval    = healResult?.final_eval ?? evalResult
  const finalPassed  = finalEval?.passed ?? false

  const scoredFields  = finalEval?.answer_key?.field_results?.filter(r => r.passed !== null) ?? []
  const passCount     = scoredFields.filter(r => r.passed === true).length
  const totalScored   = scoredFields.length

  async function run() {
    setPhase('freezing')
    setError(null)
    setEvalResult(null)
    setHealResult(null)
    try {
      // Step 1: Freeze
      const freezeRes = await fetch(`${API_BASE}/api/v1/boards/${boardId}/gate/freeze`, { method: 'POST' })
      if (!freezeRes.ok) {
        const d = await freezeRes.json().catch(() => ({}))
        throw new Error((d as { detail?: string }).detail ?? `Freeze failed (${freezeRes.status})`)
      }

      // Step 2: Codegen
      setPhase('generating')
      const codegenRes = await fetch(`${API_BASE}/api/v1/boards/${boardId}/codegen`, { method: 'POST' })
      if (!codegenRes.ok) {
        const d = await codegenRes.json().catch(() => ({}))
        throw new Error((d as { detail?: string }).detail ?? `Code generation failed (${codegenRes.status})`)
      }

      // Step 3: Eval
      setPhase('evaluating')
      const evalRes = await fetch(`${API_BASE}/api/v1/boards/${boardId}/eval`, { method: 'POST' })
      if (!evalRes.ok) {
        const d = await evalRes.json().catch(() => ({}))
        throw new Error((d as { detail?: string }).detail ?? `Evaluation failed (${evalRes.status})`)
      }
      const evalData: EvalResult = await evalRes.json()
      setEvalResult(evalData)

      if (evalData.passed) {
        setPhase('done')
        onBuilt()
        return
      }

      // Step 4: Heal
      setPhase('healing')
      const healRes = await fetch(`${API_BASE}/api/v1/boards/${boardId}/heal`, { method: 'POST' })
      if (!healRes.ok) {
        const d = await healRes.json().catch(() => ({}))
        throw new Error((d as { detail?: string }).detail ?? `Healing failed (${healRes.status})`)
      }
      const healData: HealResult = await healRes.json()
      setHealResult(healData)
      setPhase('done')
      if (healData.status !== 'agent_error') {
        onBuilt()  // agent file is on disk for all non-error statuses
      }
    } catch (err) {
      setPhase('failed')
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const overlay: React.CSSProperties = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.85)',
    zIndex: 500, display: 'flex', alignItems: 'center', justifyContent: 'center',
  }

  const modal: React.CSSProperties = {
    width: 520, maxWidth: '95vw', maxHeight: '90vh',
    background: '#0d1117', border: '1px solid #1f2937',
    borderRadius: 12, display: 'flex', flexDirection: 'column', overflow: 'hidden',
  }

  const stageDot = (s: Phase, current: Phase) => {
    const states: Phase[] = ['freezing', 'generating', 'evaluating', 'healing']
    const si = states.indexOf(s)
    const ci = states.indexOf(current as Phase)
    const isDone = ci > si || (current === 'done' || current === 'failed')
    const isActive = s === current
    return {
      width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
      background: isDone ? '#22c55e' : isActive ? '#818cf8' : '#374151',
    }
  }

  return (
    <div style={overlay}>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      <div style={modal}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', padding: '16px 20px', borderBottom: '1px solid #1f2937', flexShrink: 0 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#f9fafb' }}>Generate my agent</div>
            <div style={{ fontSize: 12, color: '#4b5563', marginTop: 2 }}>
              Build, test, and self-heal your agent against the worked example
            </div>
          </div>
          <div style={{ flex: 1 }} />
          {(phase === 'done' || phase === 'failed' || phase === 'idle') && (
            <button
              onClick={onClose}
              style={{ background: 'none', border: 'none', color: '#4b5563', fontSize: 22, cursor: 'pointer', padding: 0, lineHeight: 1 }}
              onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
              onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
            >×</button>
          )}
        </div>

        {/* Body */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '20px' }}>

          {phase === 'idle' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ fontSize: 13, color: '#9ca3af', lineHeight: 1.6 }}>
                This will:
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {[
                  ['🔒', 'Lock in your process from the whiteboard'],
                  ['✍', 'Write agent code for it'],
                  ['🧪', 'Test it against the example you uploaded'],
                  ['🔧', 'Automatically fix any issues it finds'],
                ].map(([icon, text]) => (
                  <div key={text} style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                    <span style={{ fontSize: 16, flexShrink: 0, marginTop: 1 }}>{icon}</span>
                    <span style={{ fontSize: 13, color: '#d1d5db' }}>{text}</span>
                  </div>
                ))}
              </div>
              <div style={{ fontSize: 12, color: '#4b5563', background: '#111827', borderRadius: 6, padding: '10px 12px', lineHeight: 1.5 }}>
                This can take a few minutes. Keep this window open while it runs.
              </div>
              <button
                onClick={run}
                style={{
                  padding: '11px', borderRadius: 7, fontSize: 14, fontWeight: 700,
                  background: '#1c1a00', border: '1px solid #ca8a04', color: '#fbbf24',
                  cursor: 'pointer', fontFamily: 'inherit',
                }}
                onMouseEnter={e => { e.currentTarget.style.background = '#292000'; e.currentTarget.style.color = '#fcd34d' }}
                onMouseLeave={e => { e.currentTarget.style.background = '#1c1a00'; e.currentTarget.style.color = '#fbbf24' }}
              >
                Generate my agent →
              </button>
            </div>
          )}

          {/* Running phases */}
          {(phase === 'freezing' || phase === 'generating' || phase === 'evaluating' || phase === 'healing') && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {/* Stage tracker */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {(['freezing', 'generating', 'evaluating', 'healing'] as Phase[]).map(s => {
                  const stages: Phase[] = ['freezing', 'generating', 'evaluating', 'healing']
                  const si = stages.indexOf(s)
                  const ci = stages.indexOf(phase)
                  const isActive = s === phase
                  const isPast = ci > si
                  const label: Record<string, string> = {
                    freezing: 'Locking in your process',
                    generating: 'Writing your agent',
                    evaluating: 'Testing on your example',
                    healing: 'Fixing the issues',
                  }
                  return (
                    <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <div style={stageDot(s, phase)} />
                      <span style={{
                        fontSize: 13,
                        color: isPast ? '#4ade80' : isActive ? '#f9fafb' : '#374151',
                        fontWeight: isActive ? 600 : 400,
                      }}>
                        {isPast ? '✓ ' : ''}{label[s]}
                      </span>
                      {isActive && <Spinner />}
                    </div>
                  )
                })}
              </div>

              <div style={{ fontSize: 12, color: '#4b5563', lineHeight: 1.5 }}>
                {PHASE_LABEL[phase as Exclude<Phase, 'idle' | 'done' | 'failed'>]}
                <br />
                This can take a few minutes — hang tight.
              </div>

              {/* Show what was wrong when healing */}
              {phase === 'healing' && failedFields.length > 0 && (
                <div style={{ background: '#0f0f20', border: '1px solid #312e81', borderRadius: 8, padding: '12px 14px' }}>
                  <div style={{ fontSize: 11, color: '#6b7280', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>
                    Things to fix
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                    {failedFields.map(f => (
                      <div key={f.field} style={{ fontSize: 12, color: '#c7d2fe', lineHeight: 1.4 }}>
                        · {FIELD_PLAIN[f.field] ?? f.field}: expected{' '}
                        <span style={{ color: '#4ade80' }}>{f.expected ?? '—'}</span>, got{' '}
                        <span style={{ color: '#f87171' }}>{f.actual ?? '—'}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Done */}
          {phase === 'done' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{
                  width: 40, height: 40, borderRadius: '50%', flexShrink: 0,
                  background: finalPassed ? '#14532d' : '#1c1200',
                  border: `1px solid ${finalPassed ? '#166534' : '#ca8a04'}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 20, color: finalPassed ? '#4ade80' : '#fbbf24',
                }}>
                  {finalPassed ? '✓' : '~'}
                </div>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: '#f9fafb' }}>
                    {finalPassed ? 'Your agent is ready!' : 'Agent built (with some caveats)'}
                  </div>
                  <div style={{ fontSize: 12, color: '#4b5563', marginTop: 2 }}>
                    {totalScored > 0
                      ? `${passCount} of ${totalScored} checks passing`
                      : 'No eval case to score against — built from spec'}
                  </div>
                </div>
              </div>

              {/* Heal history */}
              {healResult?.history && healResult.history.length > 0 && (
                <div style={{ background: '#0a0f1a', borderRadius: 8, padding: '12px 14px' }}>
                  <div style={{ fontSize: 11, color: '#4b5563', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 8 }}>
                    Self-heal history
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {healResult.history.map(h => (
                      <div key={h.attempt} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontSize: 11, color: h.eval_passed ? '#4ade80' : '#f87171', fontWeight: 700, width: 16 }}>
                          {h.eval_passed ? '✓' : '✗'}
                        </span>
                        <span style={{ fontSize: 12, color: '#9ca3af' }}>
                          Attempt {h.attempt}
                        </span>
                        <span style={{ fontSize: 12, color: h.eval_passed ? '#4ade80' : '#6b7280' }}>
                          {h.pass_count}/{h.total_checks} checks passed
                        </span>
                      </div>
                    ))}
                  </div>
                  {healResult.status && HEAL_STATUS_LABELS[healResult.status] && (
                    <div style={{ fontSize: 11, color: '#6b7280', marginTop: 8, borderTop: '1px solid #1f2937', paddingTop: 8 }}>
                      {HEAL_STATUS_LABELS[healResult.status]}
                    </div>
                  )}
                </div>
              )}

              {!finalPassed && (
                <div style={{ background: '#1c1200', border: '1px solid #ca8a0444', borderRadius: 7, padding: '10px 12px', fontSize: 12, color: '#fbbf24', lineHeight: 1.5 }}>
                  The agent couldn't fully match the expected result. It will still run on your inbox — the output might just be off on a few fields.
                </div>
              )}

              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  onClick={onClose}
                  style={{
                    flex: 1, padding: '10px', borderRadius: 7, fontSize: 13, fontWeight: 700,
                    background: '#14532d', border: '1px solid #16a34a', color: '#4ade80',
                    cursor: 'pointer', fontFamily: 'inherit',
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = '#166534'; e.currentTarget.style.color = '#86efac' }}
                  onMouseLeave={e => { e.currentTarget.style.background = '#14532d'; e.currentTarget.style.color = '#4ade80' }}
                >
                  Continue →
                </button>
                {!finalPassed && (
                  <button
                    onClick={run}
                    style={{
                      padding: '10px 14px', borderRadius: 7, fontSize: 13, fontWeight: 600,
                      background: 'none', border: '1px solid #374151', color: '#6b7280',
                      cursor: 'pointer', fontFamily: 'inherit',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = '#6b7280'; e.currentTarget.style.color = '#f9fafb' }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#6b7280' }}
                  >
                    Try again
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Failed */}
          {phase === 'failed' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ width: 40, height: 40, borderRadius: '50%', flexShrink: 0, background: '#1c0a0a', border: '1px solid #7f1d1d', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20, color: '#f87171' }}>✗</div>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: '#f9fafb' }}>Something went wrong</div>
                  <div style={{ fontSize: 12, color: '#4b5563', marginTop: 2 }}>The build process hit an error</div>
                </div>
              </div>

              {error && (
                <div style={{ background: '#0a0305', border: '1px solid #7f1d1d44', borderRadius: 7, padding: '10px 12px', fontSize: 12, color: '#f87171', lineHeight: 1.6, fontFamily: 'monospace', wordBreak: 'break-word' }}>
                  {error}
                </div>
              )}

              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  onClick={run}
                  style={{
                    flex: 1, padding: '10px', borderRadius: 7, fontSize: 13, fontWeight: 700,
                    background: '#312e81', border: '1px solid #4f46e5', color: '#a5b4fc',
                    cursor: 'pointer', fontFamily: 'inherit',
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = '#3730a3'; e.currentTarget.style.color = '#f9fafb' }}
                  onMouseLeave={e => { e.currentTarget.style.background = '#312e81'; e.currentTarget.style.color = '#a5b4fc' }}
                >
                  Try again
                </button>
                <button
                  onClick={onClose}
                  style={{ padding: '10px 14px', borderRadius: 7, fontSize: 13, background: 'none', border: '1px solid #374151', color: '#6b7280', cursor: 'pointer', fontFamily: 'inherit' }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = '#6b7280'; e.currentTarget.style.color = '#f9fafb' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#6b7280' }}
                >
                  Close
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
