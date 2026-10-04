import { buildNavGroups } from '@/lib/navigation'

/**
 * One top-bar crumb. With `to` it is a link to that surface ("Observe ›
 * Alerting › Rules" where Alerting and Rules open their lists); a nav
 * group ("Plan", "Observe") is not a page and stays plain text.
 */
export type Crumb = { label: string; to?: string }

/**
 * A crumb for a sidebar surface, linked to the same href the sidebar uses, so
 * the trail and the nav cannot point at different pages. A label the nav does
 * not know stays plain.
 */
export function navCrumb(slug: string | undefined, label: string): Crumb {
  if (!slug) return { label }
  for (const group of buildNavGroups(slug, undefined)) {
    const item = group.items.find((candidate) => candidate.label === label)
    if (item) return { label, to: item.href }
  }
  return { label }
}
