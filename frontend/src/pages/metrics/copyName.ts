/**
 * A free internal name for a copy of `baseName`: `<name>_copy`, then
 * `_copy_2`, `_copy_3`… past whatever `existing` already holds. The source name
 * is already a lowercase `[a-z0-9_]` identifier, so the suffix keeps it one.
 *
 * The one Duplicate naming rule, for catalog metrics and fact tables alike.
 */
export function copyName(baseName: string, existing: ReadonlySet<string>): string {
  const root = `${baseName}_copy`
  if (!existing.has(root)) return root
  let suffix = 2
  while (existing.has(`${root}_${suffix}`)) suffix += 1
  return `${root}_${suffix}`
}
