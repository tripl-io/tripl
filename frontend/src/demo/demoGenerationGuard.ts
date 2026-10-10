/**
 * Pre-flight guard for "Generate demo project".
 *
 * Generating a demo used to be a single unguarded click, so a cancel/retry loop
 * or a second visit minted another synthetic project that then aggregated into
 * the All projects roll-ups forever. The backend caps demos per creator; this
 * is the honest warning that comes BEFORE the request, pointing at Reset —
 * which the docs position as the way to refresh a demo — instead.
 */

import { countOf } from '@/lib/plural'
import type { Project } from '@/types'
import { MAX_DEMOS_PER_CREATOR } from './useDemoProvisioning'

export interface DemoGenerationWarning {
  title: string
  message: string
  confirmLabel: string
}

/** Demos this user owns, i.e. the ones that count against their cap. */
export function ownedDemoCount(projects: readonly Project[], userId: string | undefined): number {
  if (!userId) return 0
  return projects.filter((project) => project.is_demo && project.created_by_user_id === userId)
    .length
}

/**
 * The confirmation to show before generating, or null when the user owns none
 * and the click needs no friction at all. At the cap there is nothing to
 * confirm: the button is disabled first ({@link demoGenerationBlockedReason}).
 */
export function demoGenerationWarning(owned: number): DemoGenerationWarning | null {
  if (owned <= 0) return null
  return {
    title: 'Generate another demo project?',
    message:
      `You already have ${countOf(owned, 'demo project', 'demo projects')}. Resetting an existing demo from its banner's Manage demo menu ` +
      'refreshes it in place; generating another adds a separate synthetic project that ' +
      `also counts towards the totals on All projects. You can have ${MAX_DEMOS_PER_CREATOR} at most.`,
    confirmLabel: 'Generate another',
  }
}

/**
 * Why "Generate demo project" should be disabled, or null while it may run.
 * At the cap the button used to stay enabled and open a confirm
 * whose two buttons both did nothing — the only way to learn about the limit
 * was to click. The caller disables the button and shows this next to it.
 */
export function demoGenerationBlockedReason(owned: number): string | null {
  if (owned < MAX_DEMOS_PER_CREATOR) return null
  return `${owned} of ${MAX_DEMOS_PER_CREATOR} demos — reset or delete one from its banner's Manage demo menu to make another.`
}
