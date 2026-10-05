import type { ComponentType } from 'react'
import installed from '@tripl/extensions'
import { ENTERPRISE_TEASERS, visibleTeasers, type EnterpriseTeaser } from './teasers'
import type {
  ExtensionAuthPanel,
  ExtensionRoute,
  ExtensionSettingsSection,
  ExtensionShellGate,
  FrontendExtension,
} from './types'

export type * from './types'

/**
 * The frontend extensions this build carries: those of the
 * `@tripl/extensions` module. That module is a build-time alias:
 * `./none.ts` (no extensions) unless the build sets `TRIPL_EXTENSIONS_ENTRY`
 * to another module whose default export is a `FrontendExtension[]`.
 */
export const EXTENSIONS: readonly FrontendExtension[] = installed

export const extensionRoutes: readonly ExtensionRoute[] = EXTENSIONS.flatMap(
  (extension) => extension.routes ?? [],
)

export const extensionSettingsSections: readonly ExtensionSettingsSection[] = EXTENSIONS.flatMap(
  (extension) => extension.settingsSections ?? [],
)

export const extensionAuthPanels: readonly ExtensionAuthPanel[] = EXTENSIONS.flatMap(
  (extension) => extension.authPanels ?? [],
)

export const extensionShellGates: readonly ExtensionShellGate[] = EXTENSIONS.flatMap(
  (extension) => extension.shellGates ?? [],
)

export const extensionShellBanners: readonly ComponentType[] = EXTENSIONS.flatMap(
  (extension) => extension.shellBanners ?? [],
)

/** The extension settings section at `path` (under /settings), if any, and what follows its own path. */
export function extensionSettingsSection(
  path: string,
): (ExtensionSettingsSection & { subpath: string }) | undefined {
  for (const section of extensionSettingsSections) {
    if (section.item.path === path) return { ...section, subpath: '' }
    if (section.subpaths && path.startsWith(`${section.item.path}/`)) {
      return { ...section, subpath: path.slice(section.item.path.length + 1) }
    }
  }
  return undefined
}

/**
 * The Enterprise features this build does not have, shown tagged "Enterprise"
 * where they would be (`teasers.ts`). Empty in an Enterprise build.
 */
export const enterpriseTeasers: readonly EnterpriseTeaser[] = visibleTeasers(
  ENTERPRISE_TEASERS,
  extensionSettingsSections,
)

/** The Enterprise teaser at `path` (under /settings), if this build shows one. */
export function enterpriseTeaser(path: string): EnterpriseTeaser | undefined {
  return enterpriseTeasers.find((teaser) => teaser.item.path === path)
}
