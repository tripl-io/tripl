import { Chip, type ChipTone } from '@/components/primitives/chip'
import { PROJECT_ROLE_OPTIONS, ROLE_OPTIONS, type ProjectMemberRole, type Role } from '@/types'

/**
 * Either vocabulary a chip may show (F20 PR4): an ORGANIZATION role
 * (owner | admin | member, Members and Profile) or a PROJECT membership role
 * (editor | viewer | none, Project settings › Access; `none` reads "No access",
 * a member opted out of a project the organization's default would give them).
 */
export type ChipRole = Role | ProjectMemberRole

/** One tone per role, app-wide: a label maps to exactly one tone (DS-7). */
const ROLE_TONE: Readonly<Record<ChipRole, ChipTone>> = {
  owner: 'accent',
  admin: 'info',
  member: 'neutral',
  editor: 'info',
  viewer: 'neutral',
  none: 'warning',
}

function roleLabel(role: ChipRole): string {
  return (
    ROLE_OPTIONS.find((option) => option.value === role)?.label
    ?? PROJECT_ROLE_OPTIONS.find((option) => option.value === role)?.label
    ?? role
  )
}

/**
 * A member's role as a status pill. Members and Profile used to draw "Owner"
 * two ways — a 10px beige pill on one page, a larger teal one on the other —
 * each hand-rolled (ST-16).
 */
export function RoleChip({
  role,
  size = 'sm',
  className,
}: {
  role: ChipRole
  size?: 'xs' | 'sm' | 'md'
  className?: string
}) {
  return (
    <Chip tone={ROLE_TONE[role] ?? 'neutral'} size={size} className={className}>
      {roleLabel(role)}
    </Chip>
  )
}
