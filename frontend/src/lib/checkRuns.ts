import { settingsPath } from '@/lib/navigation'

/** A commit as people read it: its first seven characters. */
export function shortSha(sha: string): string {
  return sha.slice(0, 7)
}

/** Settings › Project › Check runs for one project: the history and the link template. */
export function checkRunsSettingsPath(slug: string): string {
  return settingsPath(`/settings/project/check-runs?project=${encodeURIComponent(slug)}`)
}
