import type { MetaFieldDefinition } from '@/types'
import { META_FIELD_LINK_PLACEHOLDER } from './metaFields'

/**
 * The tracker ticket a branch is named after, and the meta field that links to
 * it.
 *
 * On production every branch is named after its Jira ticket (`PROJ-4770`) and
 * every event carries a `jira` meta field whose link template turns the key
 * into a URL. Nothing joined the two: the branch page showed the key as plain
 * text and each new event asked for the key again. The join is a naming
 * convention, not a data model, so it lives here and is applied only where
 * both halves are present — a branch named anything else, or a project with no
 * linking meta field, gets nothing.
 */

/** A Jira-style key at the start of the name: `ABC-123`, `PROJ-4770-2` → `PROJ-4770`. */
const TICKET_KEY = /^[A-Z][A-Z0-9]+-\d+/

export interface BranchTicket {
  key: string
  href: string
  field: MetaFieldDefinition
}

export function ticketKeyFromBranchName(name: string): string | null {
  const match = TICKET_KEY.exec(name.trim())
  return match ? match[0] : null
}

/** The first string meta field whose link template can take a key. */
export function ticketMetaField(metaFields: MetaFieldDefinition[]): MetaFieldDefinition | null {
  return (
    metaFields.find(
      field =>
        field.field_type === 'string' &&
        typeof field.link_template === 'string' &&
        field.link_template.includes(META_FIELD_LINK_PLACEHOLDER),
    ) ?? null
  )
}

export function branchTicket(
  branchName: string | null | undefined,
  metaFields: MetaFieldDefinition[],
): BranchTicket | null {
  if (!branchName) return null
  const key = ticketKeyFromBranchName(branchName)
  const field = ticketMetaField(metaFields)
  if (!key || !field?.link_template) return null
  return { key, href: field.link_template.replaceAll(META_FIELD_LINK_PLACEHOLDER, key), field }
}
