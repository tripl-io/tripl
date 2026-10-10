/**
 * The Platform group's sections — the operator's settings, each at
 * `/settings/instance/<key>` — by the key their page and the service-settings
 * API use, in rail order, with the one name each goes by.
 *
 * One copy of the names, so the rail and the Platform pages cannot spell one
 * section two ways ("Reset Email to defaults" on the page the rail calls Mail
 * relay). Free of imports on purpose: the service-settings helpers can read it
 * without pulling in the rail's icons and extensions.
 */
export const PLATFORM_SECTION_KEYS = [
  'runtime',
  'email',
  'ai',
  'security',
  'storage',
  'observability',
  'system',
] as const

export type PlatformSectionKey = (typeof PLATFORM_SECTION_KEYS)[number]

export const PLATFORM_SECTION_LABELS: Readonly<Record<PlatformSectionKey, string>> = {
  runtime: 'Runtime',
  // Named apart from Organization › Email: this one carries account mail
  // (sign-up, password reset, invitations) and is every organization's
  // default relay.
  email: 'Mail relay',
  ai: 'AI & search',
  security: 'Security & access',
  storage: 'Storage',
  observability: 'Observability',
  system: 'System',
}

/** The settings path of a Platform section: `instance/runtime`. */
export function platformSectionPath(key: PlatformSectionKey): string {
  return `instance/${key}`
}
