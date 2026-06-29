import { useState } from 'react'
import type { GateComment } from '../types'

interface GateBubbleProps {
  comment: GateComment
  onAnswer: (id: string, answer: string) => Promise<boolean>
  onDismiss: (id: string) => Promise<boolean>
}

export function GateBubble({ comment, onAnswer, onDismiss }: GateBubbleProps) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(comment.answer ?? '')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const isBlocking  = comment.severity === 'blocking'
  const isAnswered  = comment.status === 'answered'
  const isResolved  = comment.status === 'resolved'
  const isDismissed = comment.status === 'rejected'
  // "needs detail" = the verify pass returned insufficient; status stays 'answered' but followup is set
  const needsDetail = isAnswered && !!comment.followup

  const accentColor = isBlocking ? '#ef4444' : '#f59e0b'
  const pinBg     = isDismissed ? '#374151'
                  : isResolved   ? '#166534'
                  : needsDetail  ? '#431407'   // deep orange — distinct from advisory amber
                  : isAnswered   ? '#1e3a22'
                  : accentColor
  const pinBorder = isDismissed ? '#4b5563'
                  : isResolved   ? '#22c55e'
                  : needsDetail  ? '#f97316'   // orange-500
                  : isAnswered   ? '#4ade8088'
                  : accentColor
  const pinIcon   = isDismissed ? '−'
                  : isResolved   ? '✓'
                  : needsDetail  ? '↻'         // "needs revisiting"
                  : isAnswered   ? '…'
                  : isBlocking   ? '!'
                  : '?'
  const pinOpacity = isDismissed ? 0.35 : isResolved ? 0.65 : 1

  return (
    <div style={{ position: 'relative' }}>

      {/* ── Pin ── */}
      <button
        onClick={() => setOpen(o => !o)}
        onMouseDown={e => e.stopPropagation()}
        title={comment.question}
        style={{
          width: 20, height: 20,
          borderRadius: '50%',
          background: pinBg,
          border: `2px solid ${pinBorder}`,
          color: '#fff',
          fontSize: 10, fontWeight: 800,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', padding: 0,
          opacity: pinOpacity,
          boxShadow: needsDetail            ? '0 0 0 3px #f9731630'
                   : !isAnswered && !isDismissed && !isResolved ? `0 0 0 3px ${accentColor}30`
                   : 'none',
          transition: 'opacity 0.15s, box-shadow 0.15s',
        }}
      >
        {pinIcon}
      </button>

      {/* ── Popover ── */}
      {open && (
        <div
          onMouseDown={e => e.stopPropagation()}
          style={{
            position: 'absolute',
            top: 26, right: 0,
            width: 300,
            background: '#0d1117',
            border: `1px solid ${isResolved ? '#22c55e44' : needsDetail ? '#f9731655' : accentColor + '55'}`,
            borderRadius: 10,
            padding: '12px 14px',
            zIndex: 400,
            boxShadow: `0 10px 40px rgba(0,0,0,0.8), 0 0 0 1px ${accentColor}12`,
          }}
        >
          {/* Header */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 9 }}>
            <span style={{
              fontSize: 9, color: isResolved ? '#22c55e' : needsDetail ? '#f97316' : accentColor,
              textTransform: 'uppercase', fontWeight: 800, letterSpacing: '0.08em',
            }}>
              {comment.severity}
            </span>
            <span style={{
              fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em',
              color: needsDetail ? '#f97316' : '#374151',
              fontWeight: needsDetail ? 700 : 400,
            }}>
              · {isResolved ? 'resolved ✓' : isDismissed ? 'dismissed' : needsDetail ? 'needs more detail' : isAnswered ? 'awaiting verify' : 'open'}
            </span>
            <div style={{ flex: 1 }} />
            <button
              onClick={() => setOpen(false)}
              style={{ background: 'none', border: 'none', color: '#4b5563', cursor: 'pointer', fontSize: 18, padding: 0, lineHeight: 1 }}
              onMouseEnter={e => (e.currentTarget.style.color = '#f9fafb')}
              onMouseLeave={e => (e.currentTarget.style.color = '#4b5563')}
            >×</button>
          </div>

          {/* Question */}
          <div style={{ fontSize: 12, color: '#e2e8f0', lineHeight: 1.6, marginBottom: 10 }}>
            {comment.question}
          </div>

          {/* Follow-up (when answer was insufficient) */}
          {comment.followup && !isResolved && (
            <div style={{
              background: '#160e00',
              border: '1px solid #f59e0b44',
              borderRadius: 6, padding: '7px 9px', marginBottom: 10,
            }}>
              <div style={{ fontSize: 9, color: '#f59e0b', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 3 }}>
                Needs more detail
              </div>
              <div style={{ fontSize: 11, color: '#fcd34d', lineHeight: 1.5 }}>
                {comment.followup}
              </div>
            </div>
          )}

          {/* Previous answer (when answered or resolved) */}
          {(isAnswered || isResolved) && comment.answer && (
            <div style={{
              background: '#030f07',
              border: `1px solid ${isResolved ? '#22c55e55' : '#22c55e22'}`,
              borderRadius: 6, padding: '6px 9px', marginBottom: 10,
              fontSize: 11, color: isResolved ? '#86efac' : '#4ade8099', lineHeight: 1.5,
            }}>
              <div style={{ fontSize: 9, color: isResolved ? '#22c55e' : '#166534', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 3 }}>
                {isResolved ? 'Accepted answer' : 'Your answer'}
              </div>
              {comment.answer}
            </div>
          )}

          {/* Resolved — no further action */}
          {isResolved && (
            <div style={{ fontSize: 11, color: '#166534', textAlign: 'center', padding: '4px 0' }}>
              This gap has been resolved ✓
            </div>
          )}

          {/* Answer input + actions (open, answered, or needs-followup states) */}
          {!isResolved && !isDismissed && (
            <>
              <textarea
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={e => e.stopPropagation()}
                rows={3}
                placeholder={
                  comment.followup ? 'Address the follow-up above…'
                  : isAnswered ? 'Update your answer…'
                  : 'Type your answer…'
                }
                style={{
                  width: '100%', boxSizing: 'border-box',
                  background: '#0a0f1a',
                  border: `1px solid ${submitError ? '#ef444466' : '#374151'}`,
                  borderRadius: 6, color: '#d1d5db', fontSize: 12,
                  padding: '7px 9px', outline: 'none',
                  fontFamily: 'inherit', resize: 'vertical', lineHeight: 1.5,
                }}
                onFocus={e => { e.currentTarget.style.borderColor = '#6b7280' }}
                onBlur={e => { e.currentTarget.style.borderColor = submitError ? '#ef444466' : '#374151' }}
              />

              {/* Submission error */}
              {submitError && (
                <div style={{ fontSize: 10, color: '#ef4444', marginTop: 4, marginBottom: 2 }}>
                  {submitError}
                </div>
              )}

              <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                <button
                  onClick={async () => {
                    if (!draft.trim() || submitting) return
                    setSubmitting(true)
                    setSubmitError(null)
                    const ok = await onAnswer(comment.id, draft.trim())
                    setSubmitting(false)
                    if (ok) setOpen(false)
                    else setSubmitError('Failed to save — please try again.')
                  }}
                  disabled={!draft.trim() || submitting}
                  style={{
                    flex: 1, background: submitting ? '#1e293b' : '#22c55e', border: 'none',
                    borderRadius: 5, color: '#fff', fontSize: 12, fontWeight: 600,
                    padding: '6px 10px',
                    cursor: draft.trim() && !submitting ? 'pointer' : 'default',
                    opacity: draft.trim() && !submitting ? 1 : 0.4,
                  }}
                >
                  {submitting ? 'Saving…' : isAnswered ? 'Update' : 'Answer'}
                </button>
                <button
                  onClick={async () => {
                    if (submitting) return
                    setSubmitting(true)
                    setSubmitError(null)
                    const ok = await onDismiss(comment.id)
                    setSubmitting(false)
                    if (ok) setOpen(false)
                    else setSubmitError('Failed to dismiss — please try again.')
                  }}
                  disabled={submitting}
                  style={{
                    background: 'none', border: '1px solid #374151',
                    borderRadius: 5, color: '#6b7280', fontSize: 12,
                    padding: '6px 10px', cursor: submitting ? 'default' : 'pointer',
                    fontFamily: 'inherit', opacity: submitting ? 0.4 : 1,
                  }}
                  onMouseEnter={e => { if (!submitting) { e.currentTarget.style.borderColor = '#6b7280'; e.currentTarget.style.color = '#9ca3af' } }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = '#374151'; e.currentTarget.style.color = '#6b7280' }}
                >
                  Dismiss
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
