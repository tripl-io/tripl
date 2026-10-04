import type { ComponentType, LazyExoticComponent, ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import type { SettingsNavItem } from '@/components/settings/nav'
import type { PageSkeletonVariant } from '@/components/states/skeletons'
import type { OrgMembership } from '@/types'

/**
 * What a frontend extension adds to the app. The SPA renders these without
 * knowing what they are; the backend's counterpart is `tripl.extensions`.
 * Every field is optional: an extension fills only what it needs.
 */
export interface FrontendExtension {
  name: string
  /** Top-level routes outside the app shell (public pages, callbacks). */
  routes?: readonly ExtensionRoute[]
  /** Settings sections: a rail item plus the page it opens. */
  settingsSections?: readonly ExtensionSettingsSection[]
  /** Extra ways to sign in, offered under the password form on /auth. */
  authPanels?: readonly ExtensionAuthPanel[]
  /** Screens that replace the app shell when a request inside it is refused. */
  shellGates?: readonly ExtensionShellGate[]
}

export interface ExtensionRoute {
  path: string
  /** Suspense key of the route while its chunk loads. */
  key: string
  skeleton?: PageSkeletonVariant
  Component: LazyExoticComponent<ComponentType>
}

export interface ExtensionSettingsSection {
  /** The rail group the item joins, by label ("Organization"). */
  group: string
  /** The id of the item to place it after; appended to the group when absent. */
  after?: string
  /** The rail item. Its `path` is the section's route under /settings. */
  item: SettingsNavItem
  /**
   * Who may open the page: `orgOwner` is an organization owner only (not an
   * admin); `owner` is an owner or admin. Anyone else sees a read-only notice.
   */
  access: 'orgOwner' | 'owner'
  /** For `orgOwner`: what the page is and why it is an owner's. */
  deniedReason?: string
  Component: LazyExoticComponent<ComponentType>
}

export interface ExtensionAuthPanelProps {
  email: string
  onEmailChange: (email: string) => void
  /** Where to go after signing in (a same-origin path). */
  next: string
  onBack: () => void
}

export interface ExtensionAuthPanel {
  id: string
  /** The button under the password form that opens the panel. */
  buttonLabel: string
  icon: LucideIcon
  title: string
  description: string
  Component: LazyExoticComponent<ComponentType<ExtensionAuthPanelProps>>
}

export interface ExtensionShellGateContext {
  /** The shell's own request errors (project list, project confirmation). */
  errors: readonly unknown[]
  orgName: string
  orgSlug: string | null
  /** Where to come back to (the current same-origin path). */
  returnTo: string
  /** The user's other organizations, for a way out. */
  otherOrgs: readonly OrgMembership[]
}

/** Returns a screen to show instead of the shell, or null to let the shell render. */
export type ExtensionShellGate = (context: ExtensionShellGateContext) => ReactNode | null
