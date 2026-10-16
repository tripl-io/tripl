/**
 * The demo recipe this build's server seeds. Mirrors
 * `services/demo/scenario.py:DEMO_RECIPE_VERSION` in the backend; bump both
 * together.
 *
 * Version 5 moved demos onto the shared demo warehouse: their history is
 * scanned from it and kept moving by the scheduled collection. A demo seeded
 * by an older recipe sits on its own synthetic source, which nothing advances
 * any more, so it is told apart and offered a rebuild.
 */
export const CURRENT_DEMO_RECIPE_VERSION = 5

/**
 * Whether a demo was built by a recipe older than this build's. The server
 * sends "4"; an older one sent "v3". A demo with no version, or one this build
 * cannot read, is not called outdated: nagging about a version nobody can
 * name would offer a rebuild nobody asked for.
 */
export function isOutdatedDemoRecipe(version: string | null | undefined): boolean {
  const digits = version?.trim().replace(/^v/i, '')
  if (!digits || !/^\d+$/.test(digits)) return false
  return Number(digits) < CURRENT_DEMO_RECIPE_VERSION
}
