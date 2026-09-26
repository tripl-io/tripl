import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/** Scroll the event's discussion into view and focus its composer. */
function focusDiscussionComposer() {
  window.setTimeout(() => {
    document.getElementById('event-discussion')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    document.getElementById('event-detail-discussion-body')?.focus({ preventScroll: true })
  }, 0)
}

export interface CommentDraft {
  text: string
  /** Bumped per hand-over: the discussion is keyed on it, so it remounts with the new draft. */
  seq: number
}

/**
 * A tracking-bug verdict's "Open a comment on the event" (#254), from this
 * page or from elsewhere.
 *
 * The Signal card calls `startDraft`: the discussion remounts with the draft
 * in its composer, scrolled to and focused. The Anomalies row menu's "Comment
 * on the event" arrives with the draft in the navigation state instead: it is
 * taken once, then the state is dropped so Back or a reload does not start it
 * again — the same handoff as the row's Annotate (`useAnnotateHandoff`).
 * `ready` is whether the event page has painted past its skeleton, so the
 * focus waits for the composer to exist.
 */
export function useCommentDraftHandoff({ ready }: { ready: boolean }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [draft, setDraft] = useState<CommentDraft | null>(null)
  const startDraft = (text: string) => {
    setDraft((previous) => ({ text, seq: (previous?.seq ?? 0) + 1 }))
    focusDiscussionComposer()
  }

  const pendingDraft = (location.state as { commentDraft?: unknown } | null)?.commentDraft
  // Taken while rendering (the adjust-state-on-prop-change pattern); the
  // effects only drop the navigation state and move focus.
  const [takenDraft, setTakenDraft] = useState<string | null>(null)
  if (typeof pendingDraft === 'string' && pendingDraft !== takenDraft) {
    setTakenDraft(pendingDraft)
    setDraft((previous) => ({ text: pendingDraft, seq: (previous?.seq ?? 0) + 1 }))
  } else if (typeof pendingDraft !== 'string' && takenDraft !== null) {
    setTakenDraft(null)
  }
  const focusPending = useRef(false)
  useEffect(() => {
    if (typeof pendingDraft !== 'string') return
    void navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
    focusPending.current = true
  }, [pendingDraft, navigate, location.pathname, location.search])
  useEffect(() => {
    if (!ready || !focusPending.current) return
    focusPending.current = false
    focusDiscussionComposer()
  }, [ready, pendingDraft])

  return { commentDraft: draft, startDraft }
}
