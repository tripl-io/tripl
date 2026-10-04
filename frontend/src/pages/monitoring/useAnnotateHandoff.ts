import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/** Scroll the Volume tab's annotation form into view and focus its label. */
function focusAnnotationForm() {
  window.setTimeout(() => {
    document.getElementById('chart-annotations')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    document.getElementById('annotation-label')?.focus({ preventScroll: true })
  }, 0)
}

/**
 * Starting an annotation on a flagged bucket, from this page or from elsewhere.
 *
 * The signal banner's "Annotate" calls `startAnnotation`: the Volume
 * tab, the form prefilled with the bucket, and focus in its label field. The
 * Anomalies row menu's "Annotate" arrives with the bucket in the
 * navigation state instead: the same annotation starts once, then the state is
 * dropped so Back or a reload does not start it again.
 *
 * `ready` is whether the page has painted its Volume tab rather than a
 * skeleton. A metric page waits on its definition and an event page on the
 * event, so focusing on arrival found no form, and the handoff looked ignored
 * on the metric scope. The focus now waits for `ready`.
 */
export function useAnnotateHandoff({
  ready,
  showVolumeTab,
}: {
  ready: boolean
  showVolumeTab: () => void
}) {
  const navigate = useNavigate()
  const location = useLocation()
  const [annotatePrefill, setAnnotatePrefill] = useState<string | null>(null)
  const startAnnotation = (bucket: string) => {
    showVolumeTab()
    setAnnotatePrefill(bucket)
    focusAnnotationForm()
  }

  const pendingAnnotateBucket = (location.state as { annotateBucket?: unknown } | null)?.annotateBucket
  // The prefill is taken while rendering (the adjust-state-on-prop-change
  // pattern); the effects only rewrite the URL (Volume tab, no state) and move
  // focus, so they set no React state.
  const [takenAnnotateBucket, setTakenAnnotateBucket] = useState<string | null>(null)
  if (typeof pendingAnnotateBucket === 'string' && pendingAnnotateBucket !== takenAnnotateBucket) {
    setTakenAnnotateBucket(pendingAnnotateBucket)
    setAnnotatePrefill(pendingAnnotateBucket)
  } else if (typeof pendingAnnotateBucket !== 'string' && takenAnnotateBucket !== null) {
    setTakenAnnotateBucket(null)
  }
  const focusPending = useRef(false)
  useEffect(() => {
    if (typeof pendingAnnotateBucket !== 'string') return
    const params = new URLSearchParams(location.search)
    params.delete('tab')
    const query = params.toString()
    void navigate(`${location.pathname}${query ? `?${query}` : ''}`, { replace: true, state: null })
    focusPending.current = true
  }, [pendingAnnotateBucket, navigate, location.pathname, location.search])
  useEffect(() => {
    if (!ready || !focusPending.current) return
    focusPending.current = false
    focusAnnotationForm()
  }, [ready, pendingAnnotateBucket])

  return { annotatePrefill, startAnnotation }
}
