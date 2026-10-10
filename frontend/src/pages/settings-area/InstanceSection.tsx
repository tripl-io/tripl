import { Suspense } from 'react'
import { Navigate } from 'react-router-dom'
import { lazyWithReload } from '@/lib/lazyWithReload'
import { SHeader } from '@/components/settings/kit'
import {
  PLATFORM_SECTION_KEYS,
  PLATFORM_SECTION_LABELS,
  platformSectionPath,
  type PlatformSectionKey,
} from '@/components/settings/platform-sections'
import { InstanceSettingsSkeleton } from '@/pages/settings-service/ServiceSettingsPrimitives'

const ServiceSettingsSection = lazyWithReload(() => import('@/pages/ServiceSettingsPage'))

/**
 * What each section is for, under its title. The title is the rail's name for
 * it (platform-sections.ts), so the two cannot drift apart.
 *
 * No "takes effect on the next deploy" line on Runtime: its fields are read
 * fresh at request/task time. Each section states its own timing in the save
 * bar instead (applyNote).
 */
const DESCRIPTIONS: Record<PlatformSectionKey, string> = {
  runtime:
    'Core server configuration, and the row limits organizations inherit and may not exceed.',
  email:
    'The relay account mail (sign-up, password reset, invitations) always uses, and the one every organization without its own sends alerts and digests through.',
  ai: 'The AI provider every organization without its own inherits, and the search embeddings every organization uses.',
  security: 'Authentication and network policy for everyone on this instance.',
  // Photos only: events live in the database, and nothing here touches them.
  storage:
    'Where event photos are stored, and what an upload may be. Organizations without storage of their own use this store.',
  observability: 'How tripl reports its own health to your monitoring stack.',
  system: 'Read-only health and build information for this instance.',
}

function isPlatformSection(section: string): section is PlatformSectionKey {
  return (PLATFORM_SECTION_KEYS as readonly string[]).includes(section)
}

/**
 * Platform (platform admins only, F20 PR9). Frames the ServiceSettingsSection
 * component, which self-fetches, gates on the platform-admin flag and owns all
 * field wiring and mutations, with the section header.
 */
export default function InstanceSection({ section }: { section: string }) {
  // An unknown section used to render Runtime under a URL (and a rail
  // highlight) that said otherwise. Correct the URL instead.
  if (!isPlatformSection(section)) {
    return <Navigate to={`/settings/${platformSectionPath('runtime')}`} replace />
  }
  return (
    <div>
      <SHeader title={PLATFORM_SECTION_LABELS[section]} description={DESCRIPTIONS[section]} />
      <Suspense fallback={<InstanceSettingsSkeleton />}>
        <ServiceSettingsSection section={section} />
      </Suspense>
    </div>
  )
}
