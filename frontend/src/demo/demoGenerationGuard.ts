/**
 * Pre-flight guard for "Generate demo project".
 *
 * Generating a demo used to be a single unguarded click, so a cancel/retry loop
 * or a second visit minted another synthetic workspace that then aggregated into
 * the real workspace roll-ups forever. The backend caps demos per creator; this
 * is the honest warning that comes BEFORE the request, pointing at Reset —
 * which the docs position as the way to refresh a demo — instead.
 */

import type { Project } from '@/types'
import { MAX_DEMOS_PER_CREATOR } from './useDemoProvisioning'

export interface DemoGenerationWarning {
  title: string
  message: string
  confirmLabel: string
  /** False when the backend would refuse the create outright (cap reached). */
  canProceed: boolean
}

/** Demos this user owns, i.e. the ones that count against their cap. */
export function ownedDemoCount(projects: readonly Project[], userId: string | undefined): number {
  if (!userId) return 0
  return projects.filter((project) => project.is_demo && project.created_by_user_id === userId)
    .length
}

/**
 * The confirmation to show before generating, or null when the user owns none
 * and the click needs no friction at all.
 */
export function demoGenerationWarning(owned: number): DemoGenerationWarning | null {
  if (owned <= 0) return null
  if (owned >= MAX_DEMOS_PER_CREATOR) {
    return {
      title: 'Demo limit reached',
      message:
        `You already have ${owned} demo workspaces, which is the limit. ` +
        "Reset one from its demo banner's Manage demo menu to get a fresh copy, or delete one first.",
      confirmLabel: 'OK',
      canProceed: false,
    }
  }
  const plural = owned === 1 ? 'demo workspace' : 'demo workspaces'
  return {
    title: 'Generate another demo workspace?',
    message:
      `You already have ${owned} ${plural}. Resetting an existing demo from its banner's Manage demo menu ` +
      'refreshes it in place; generating another adds a separate synthetic project that ' +
      `also counts towards this workspace's totals. You can have ${MAX_DEMOS_PER_CREATOR} at most.`,
    confirmLabel: 'Generate another',
    canProceed: true,
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
