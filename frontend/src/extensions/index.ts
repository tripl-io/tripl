import installed from '@tripl/extensions'
import { bundledEnterprise } from './bundled'
import type {
  ExtensionAuthPanel,
  ExtensionRoute,
  ExtensionSettingsSection,
  ExtensionShellGate,
  FrontendExtension,
} from './types'

export type * from './types'

/**
 * The frontend extensions this build carries: the bundled one, then those of
 * the `@tripl/extensions` module. That module is a build-time alias:
 * `./none.ts` (no extensions) unless the build sets `TRIPL_EXTENSIONS_ENTRY`
 * to another module whose default export is a `FrontendExtension[]`.
 */
export const EXTENSIONS: readonly FrontendExtension[] = [bundledEnterprise, ...installed]

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

/** The extension settings section at `path` (under /settings), if any. */
export function extensionSettingsSection(path: string): ExtensionSettingsSection | undefined {
  return extensionSettingsSections.find((section) => section.item.path === path)
}
