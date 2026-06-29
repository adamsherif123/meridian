import type { CSSProperties } from 'react'
import type { AgentRun, FlowState, FrozenSpec, GateComment } from '../types'

const RESULT_META: Record<string, { label: string; color: string }> = {
  invoices_processed: { label: 'Invoices found',         color: '#60a5fa' },
  invoices_succeeded: { label: 'Invoices passed',        color: '#4ade80' },
  invoices_failed:    { label: 'Invoices failed',        color: '#f87171' },
  goods_failed:       { label: 'Products with issues',   color: '#fbbf24' },
  batches_processed:  { label: 'Batches checked',        color: '#60a5fa' },
  batches_succeeded:  { label: 'Certificates matched',   color: '#4ade80' },
  batches_failed:     { label: 'Certificates unmatched', color: '#f87171' },
}

interface Props {
  boardId: string | null
  flowState: FlowState | null
  gateComments: GateComment[]
  gateRunning: boolean
  verifying: boolean
  frozenSpec: FrozenSpec | null
  runLiveRunning: boolean
  runLiveResult: AgentRun | null
  runLiveError: string | null
  gateError: string | null
  onRunGate(): void
  onVerifyAnswers(): void
  onOpenWorkedExample(): void
  onOpenBuildOrchestrator(): void
  onRunLive(force?: boolean): void
  onShowSpec(): void
  onDismissRun(): void
}

export function GuidedPanel({
  boardId, flowState, gateComments, gateRunning, verifying, frozenSpec,
  runLiveRunning, runLiveResult, runLiveError, gateError,
  onRunGate, onVerifyAnswers, onOpenWorkedExample, onOpenBuildOrchestrator,
  onRunLive, onShowSpec, onDismissRun,
}: Props) {
  if (!boardId) return null

  const openBlocking  = gateComments.filter(c => c.severity === 'blocking' && c.status === 'open').length
  const openAdvisory  = gateComments.filter(c => c.severity === 'advisory' && c.status === 'open').length
  const answeredCount = gateComments.filter(c => c.status === 'answered').length
  const needsDetail   = gateComments.filter(c => c.status === 'answered' && !!c.followup).length
  const resolvedCount = gateComments.filter(c => ['resolved', 'rejected'].includes(c.status)).length
  const totalComments = gateComments.length

  function downloadCsv() {
    if (!runLiveResult?.csv_content) return
    const shipNum = runLiveResult.result_json?.shipment_number
    const name = shipNum
      ? `meridian-result-${shipNum}.csv`
      : `meridian-result-${Date.now()}.csv`
    const blob = new Blob([runLiveResult.csv_content], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = String(name)
    a.click()
    URL.revokeObjectURL(url)
  }

  const activeStep = (() => {
    if (!flowState || !flowState.ai_check_done) return 1
    if (flowState.blocking_questions_open > 0) return 2
    if (!flowState.worked_example_captured) return 3
    if (!flowState.agent_built) return 4
    return 5
  })()

  const locked = (s: number) => {
    if (!flowState) return s > 1
    switch (s) {
      case 1: return false
      case 2: return !flowState.ai_check_done
      case 3: return !flowState.ai_check_done || flowState.blocking_questions_open > 0
      case 4: return !flowState.worked_example_captured
      case 5: return !flowState.agent_built
      default: return true
    }
  }

  const done = (s: number) => {
    if (!flowState) return false
    switch (s) {
      case 1: return flowState.ai_check_done
      case 2: return flowState.ai_check_done && flowState.blocking_questions_open === 0
      case 3: return flowState.worked_example_captured
      case 4: return flowState.agent_built
      case 5: return !!runLiveResult
      default: return false
    }
  }

  const badge = (n: number): CSSProperties => {
    const l = locked(n), d = done(n), a = activeStep === n && !d
    return {
      width: 22, height: 22, borderRadius: '50%',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: 11, fontWeight: 700, flexShrink: 0,
      background: l ? '#111827' : d ? '#14532d' : a ? '#4338ca' : '#1e1b4b',
      color: l ? '#374151' : d ? '#4ade80' : '#c7d2fe',
      border: `1px solid ${l ? '#1f2937' : d ? '#166534' : a ? '#4f46e5' : '#312e81'}`,
    }
  }

  const titleStyle = (n: number): CSSProperties => ({
    fontSize: 13, fontWeight: activeStep === n && !done(n) ? 600 : 400, flex: 1,
    color: locked(n) ? '#374151' : done(n) ? '#6b7280' : '#f9fafb',
  })

  const stepBox = (n: number): CSSProperties => ({
    padding: '12px 14px',
    borderBottom: n < 5 ? '1px solid #1f2937' : 'none',
    background: activeStep === n && !done(n) ? 'rgba(79,70,229,0.04)' : 'transparent',
    opacity: locked(n) ? 0.4 : 1,
    transition: 'opacity 0.2s',
  })

  const actionBtn = (color: 'indigo' | 'green' | 'amber', dis?: boolean): CSSProperties => {
    const map = {
      indigo: { bg: '#312e81', brd: '#4f46e5', txt: '#a5b4fc' },
      green:  { bg: '#052e16', brd: '#16a34a', txt: '#4ade80' },
      amber:  { bg: '#1c1a00', brd: '#ca8a04', txt: '#fbbf24' },
    }
    const c = map[color]
    return {
      width: '100%', padding: '7px 10px', borderRadius: 6, fontSize: 12, fontWeight: 600,
      background: dis ? '#1e293b' : c.bg, border: `1px solid ${dis ? '#374151' : c.brd}`,
      color: dis ? '#4b5563' : c.txt, cursor: dis ? 'default' : 'pointer', marginTop: 8,
      fontFamily: 'inherit',
    }
  }

  return (
    <div style={{
      width: 258, flexShrink: 0, borderLeft: '1px solid #1f2937',
      background: '#030712', display: 'flex', flexDirection: 'column', overflowY: 'auto',
    }}>
      {/* Header */}
      <div style={{ padding: '12px 14px 10px', borderBottom: '1px solid #1f2937' }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: '#f9fafb', letterSpacing: '-0.01em' }}>
          Build your agent
        </div>
        <div style={{ fontSize: 11, color: '#4b5563', marginTop: 2 }}>
          Complete each step in order
        </div>
      </div>

      {/* Step 1 — Check my process */}
      <div style={stepBox(1)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={badge(1)}>{done(1) ? '✓' : '1'}</div>
          <span style={titleStyle(1)}>Check my process</span>
        </div>
        {activeStep === 1 && (
          <>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5, margin: '6px 0', paddingLeft: 30 }}>
              Have the AI look over your whiteboard for gaps or missing details.
            </div>
            {gateError && (
              <div style={{ fontSize: 11, color: '#f87171', paddingLeft: 30, marginBottom: 4 }}>✗ {gateError}</div>
            )}
            <button
              onClick={onRunGate} disabled={gateRunning} style={actionBtn('indigo', gateRunning)}
              onMouseEnter={e => { if (!gateRunning) { e.currentTarget.style.background = '#3730a3'; e.currentTarget.style.color = '#f9fafb' } }}
              onMouseLeave={e => { if (!gateRunning) { e.currentTarget.style.background = '#312e81'; e.currentTarget.style.color = '#a5b4fc' } }}
            >
              {gateRunning ? '⏳ Checking…' : '✦ Check my process'}
            </button>
          </>
        )}
        {done(1) && activeStep !== 1 && (
          <div style={{ fontSize: 11, color: '#4b5563', marginTop: 3, paddingLeft: 30 }}>
            {totalComments > 0 ? `${totalComments} question${totalComments !== 1 ? 's' : ''} from AI` : 'No issues found'}
          </div>
        )}
      </div>

      {/* Step 2 — Answer questions */}
      <div style={stepBox(2)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={badge(2)}>{done(2) ? '✓' : '2'}</div>
          <span style={titleStyle(2)}>Answer any questions</span>
        </div>
        {!locked(2) && activeStep === 2 && (
          <>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5, margin: '6px 0', paddingLeft: 30 }}>
              Look for the question bubbles pinned to items on the canvas.
            </div>
            <div style={{ paddingLeft: 30, display: 'flex', flexDirection: 'column', gap: 3 }}>
              {openBlocking > 0 && (
                <span style={{ fontSize: 11, color: '#f87171' }}>
                  {openBlocking} blocking question{openBlocking !== 1 ? 's' : ''} still open
                </span>
              )}
              {openAdvisory > 0 && (
                <span style={{ fontSize: 11, color: '#f59e0b' }}>
                  {openAdvisory} advisory question{openAdvisory !== 1 ? 's' : ''}
                </span>
              )}
              {needsDetail > 0 && (
                <span style={{ fontSize: 11, color: '#f97316' }}>
                  {needsDetail} answer{needsDetail !== 1 ? 's' : ''} need more detail
                </span>
              )}
              {resolvedCount > 0 && openBlocking === 0 && (
                <span style={{ fontSize: 11, color: '#4ade80' }}>
                  {resolvedCount} resolved
                </span>
              )}
            </div>
            {answeredCount > 0 && (
              <button
                onClick={onVerifyAnswers} disabled={verifying} style={actionBtn('green', verifying)}
                onMouseEnter={e => { if (!verifying) { e.currentTarget.style.background = '#14532d'; e.currentTarget.style.color = '#86efac' } }}
                onMouseLeave={e => { if (!verifying) { e.currentTarget.style.background = '#052e16'; e.currentTarget.style.color = '#4ade80' } }}
              >
                {verifying ? '⏳ Verifying…' : `✓ Verify ${answeredCount} answer${answeredCount !== 1 ? 's' : ''}`}
              </button>
            )}
          </>
        )}
        {done(2) && activeStep !== 2 && !locked(2) && (
          <div style={{ fontSize: 11, color: '#4b5563', marginTop: 3, paddingLeft: 30 }}>
            {resolvedCount > 0 ? `${resolvedCount} resolved` : 'No blocking questions'}
          </div>
        )}
      </div>

      {/* Step 3 — Upload worked example */}
      <div style={stepBox(3)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={badge(3)}>{done(3) ? '✓' : '3'}</div>
          <span style={titleStyle(3)}>Upload a worked example</span>
        </div>
        {!locked(3) && activeStep === 3 && (
          <>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5, margin: '6px 0', paddingLeft: 30 }}>
              Give your agent a real email with attachments, plus what the correct result should be.
            </div>
            <button
              onClick={onOpenWorkedExample} style={actionBtn('indigo')}
              onMouseEnter={e => { e.currentTarget.style.background = '#3730a3'; e.currentTarget.style.color = '#f9fafb' }}
              onMouseLeave={e => { e.currentTarget.style.background = '#312e81'; e.currentTarget.style.color = '#a5b4fc' }}
            >
              Upload example →
            </button>
          </>
        )}
        {done(3) && (
          <div style={{ fontSize: 11, color: '#4b5563', marginTop: 3, paddingLeft: 30 }}>Example uploaded</div>
        )}
      </div>

      {/* Step 4 — Generate agent */}
      <div style={stepBox(4)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={badge(4)}>{done(4) ? '✓' : '4'}</div>
          <span style={titleStyle(4)}>Generate my agent</span>
        </div>
        {!locked(4) && activeStep === 4 && (
          <>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5, margin: '6px 0', paddingLeft: 30 }}>
              Build and automatically self-test your agent against the example you uploaded.
            </div>
            <button
              onClick={onOpenBuildOrchestrator} style={actionBtn('amber')}
              onMouseEnter={e => { e.currentTarget.style.background = '#292000'; e.currentTarget.style.color = '#fcd34d' }}
              onMouseLeave={e => { e.currentTarget.style.background = '#1c1a00'; e.currentTarget.style.color = '#fbbf24' }}
            >
              Generate my agent →
            </button>
          </>
        )}
        {done(4) && (
          <div style={{ fontSize: 11, color: '#4b5563', marginTop: 3, paddingLeft: 30, display: 'flex', alignItems: 'center', gap: 6 }}>
            Agent ready
            {frozenSpec && (
              <button
                onClick={onShowSpec}
                style={{ background: 'none', border: 'none', color: '#4f46e5', fontSize: 11, cursor: 'pointer', padding: 0, fontFamily: 'inherit' }}
              >
                · view spec
              </button>
            )}
          </div>
        )}
      </div>

      {/* Step 5 — Run on inbox */}
      <div style={{ ...stepBox(5), flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={badge(5)}>{done(5) ? '✓' : '5'}</div>
          <span style={titleStyle(5)}>Run on my inbox</span>
        </div>

        {!locked(5) && !runLiveResult && (
          <>
            <div style={{ fontSize: 11, color: '#6b7280', lineHeight: 1.5, margin: '6px 0', paddingLeft: 30 }}>
              Fetch the latest email from your Gmail inbox and run your agent on it.
            </div>
            {runLiveError && (
              <div style={{ fontSize: 11, color: '#f87171', paddingLeft: 30, marginBottom: 4 }}>✗ {runLiveError}</div>
            )}
            <button
              onClick={() => onRunLive(true)} disabled={runLiveRunning} style={actionBtn('green', runLiveRunning)}
              onMouseEnter={e => { if (!runLiveRunning) { e.currentTarget.style.background = '#14532d'; e.currentTarget.style.color = '#86efac' } }}
              onMouseLeave={e => { if (!runLiveRunning) { e.currentTarget.style.background = '#052e16'; e.currentTarget.style.color = '#4ade80' } }}
            >
              {runLiveRunning ? '⏳ Running…' : '▶ Run on my inbox'}
            </button>
            {runLiveRunning && (
              <div style={{ fontSize: 11, color: '#4b5563', marginTop: 6, paddingLeft: 30, lineHeight: 1.5 }}>
                Fetching your latest email and running your agent on it. This takes a minute or two…
              </div>
            )}
          </>
        )}

        {/* Result card */}
        {runLiveResult && (
          <div style={{ paddingLeft: 30, marginTop: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#4ade80', marginBottom: 6 }}>✓ Run complete</div>
            {runLiveResult.subject && (
              <div style={{ fontSize: 11, color: '#9ca3af', marginBottom: 8, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={runLiveResult.subject}>
                {runLiveResult.subject}
              </div>
            )}
            {runLiveResult.result_json && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {Object.entries(RESULT_META).map(([field, meta]) => {
                  const val = (runLiveResult.result_json as unknown as Record<string, unknown>)[field]
                  if (val == null) return null
                  return (
                    <div key={field} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                      <span style={{ fontSize: 11, color: '#6b7280' }}>{meta.label}</span>
                      <span style={{ fontSize: 14, fontWeight: 700, color: meta.color, marginLeft: 8 }}>{String(val)}</span>
                    </div>
                  )
                })}
              </div>
            )}
            {runLiveResult.csv_content && (
              <button
                onClick={downloadCsv}
                style={{ width: '100%', marginTop: 10, padding: '6px 8px', borderRadius: 5, fontSize: 11, fontWeight: 600, background: 'none', border: '1px solid #374151', color: '#9ca3af', cursor: 'pointer', fontFamily: 'inherit' }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = '#4f46e5'; e.currentTarget.style.color = '#a5b4fc' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#9ca3af' }}
              >
                ↓ Download CSV
              </button>
            )}
            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
              <button
                onClick={() => onRunLive(true)} disabled={runLiveRunning}
                style={{ flex: 1, padding: '6px 8px', borderRadius: 5, fontSize: 11, fontWeight: 600, background: '#052e16', border: '1px solid #16a34a', color: '#4ade80', cursor: 'pointer', fontFamily: 'inherit' }}
              >
                Run again
              </button>
              <button
                onClick={onDismissRun}
                style={{ padding: '6px 8px', borderRadius: 5, fontSize: 11, background: 'none', border: '1px solid #374151', color: '#6b7280', cursor: 'pointer', fontFamily: 'inherit' }}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
