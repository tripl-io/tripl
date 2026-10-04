import type { ConfirmOptions } from '@/hooks/useConfirm'

/**
 * The one confirmation both "Delete project" entry points ask — a project
 * card's menu on the workspace page and the danger zone in Project settings ›
 * General. They used to word it differently, and the workspace copy only said
 * "event types and events" go, which undersold the heaviest action in the
 * product. The slug has to be typed before Delete arms, and the delete
 * runs inside the dialog so a refusal shows there.
 */
export function deleteProjectConfirmation(
  project: { name: string; slug: string },
  action: () => Promise<unknown>,
): ConfirmOptions {
  return {
    title: 'Delete project',
    message: `Permanently delete “${project.name}”? Its tracking plan (event types, events, fields and properties), scans, metrics, monitors, alert rules and the whole history go with it. This cannot be undone.`,
    confirmLabel: 'Delete project',
    pendingLabel: 'Deleting…',
    errorPrefix: 'Could not delete the project',
    variant: 'danger',
    requireText: project.slug,
    action,
  }
}
