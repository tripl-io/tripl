import { Suspense } from 'react'
import { useActiveOrg } from '@/components/active-org-context'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { shouldShowOrgSwitcher } from './org-switcher-model'

// On demand: most people are in one organization and never see a switcher, so
// its menu does not ride on everyone's first load (scripts/check-bundle-budget).
const OrgSwitcher = lazyWithReload(() => import('./org-switcher').then((m) => ({ default: m.OrgSwitcher })))

/** The organization switcher, drawn only for someone in more than one organization. */
export function OrgSwitcherSlot({ compact = false }: { compact?: boolean }) {
  const { orgs } = useActiveOrg()
  if (!shouldShowOrgSwitcher(orgs.length)) return null
  return (
    <Suspense fallback={null}>
      <OrgSwitcher compact={compact} />
    </Suspense>
  )
}
