import { useContext, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Eye } from 'lucide-react'
import { platformApi } from '@/api/platform'
import { useActiveOrg } from '@/components/active-org-context'
import { AuthContext } from '@/components/auth-context'
import { Button } from '@/components/ui/button'
import { activeStepInFor, msUntil, stepInEndTime } from '@/lib/orgStatus'

/**
 * The read-only step-in banner (F20): shown across the app shell while the
 * signed-in platform admin has an unexpired step-in to the organization on
 * screen. Writes there are refused by the server ("Step-in is read-only",
 * surfaced by the usual error toast); this says so up front, says when it ends,
 * and ends it early.
 *
 * Ending it uses the id `/auth/me` carries; without one it looks the id up in
 * the caller's active step-ins first.
 */
export function StepInBanner() {
  // Read softly: shells mounted without a session (tests) have no step-in.
  const auth = useContext(AuthContext)
  const { slug, membership } = useActiveOrg()
  const navigate = useNavigate()
  const stepIn = activeStepInFor(auth?.user, slug)
  const [ending, setEnding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const expiresAt = stepIn?.expires_at

  // At the end of the step-in the session changes: ask for it again, so the
  // banner goes and the organization stops answering.
  // The provider hands out a new `refresh` every render; the timer keys on the
  // expiry alone.
  const refreshRef = useRef(auth?.refresh)
  useEffect(() => {
    refreshRef.current = auth?.refresh
  })
  useEffect(() => {
    if (!expiresAt) return
    const delay = msUntil(expiresAt)
    if (!Number.isFinite(delay) || delay <= 0) return
    // setTimeout overflows past ~24.8 days; a step-in lasts at most 4 hours.
    const timer = window.setTimeout(() => refreshRef.current?.(), Math.min(delay + 1000, 2 ** 31 - 1))
    return () => window.clearTimeout(timer)
  }, [expiresAt])

  if (!stepIn || !slug || !auth) return null

  const orgName = membership?.name ?? slug

  const endNow = async () => {
    setEnding(true)
    setError(null)
    try {
      let id = stepIn.id
      if (!id) {
        const active = await platformApi.listStepIns(true)
        id = active.find((candidate) => candidate.org_slug === slug)?.id
      }
      if (id) await platformApi.endStepIn(id)
      auth.refresh()
      void navigate('/settings/platform/orgs')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not end the step-in.')
      setEnding(false)
    }
  }

  return (
    <div
      role="status"
      data-testid="step-in-banner"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-body-sm"
      style={{
        background: 'var(--warning-soft, var(--bg-sunken))',
        borderColor: 'var(--border)',
        color: 'var(--fg)',
      }}
    >
      <Eye aria-hidden="true" className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        Read-only step-in to <strong>{orgName}</strong> — ends at {stepInEndTime(stepIn.expires_at)}
        {error && (
          <span className="ml-2" style={{ color: 'var(--danger)' }}>
            {error}
          </span>
        )}
      </span>
      <Button type="button" size="xs" variant="outline" onClick={() => void endNow()} disabled={ending}>
        {ending ? 'Ending…' : 'End now'}
      </Button>
    </div>
  )
}
