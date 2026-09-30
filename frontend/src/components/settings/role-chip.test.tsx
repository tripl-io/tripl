import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RoleChip, type ChipRole } from './role-chip'

describe('RoleChip', () => {
  // One tone per role, app-wide (DS-7): Members and Profile must not drift.
  // Both vocabularies: the organization's (owner | admin | member) and a
  // project membership's (editor | viewer | none).
  it.each<[ChipRole, string, string]>([
    ['owner', 'Owner', 'accent'],
    ['admin', 'Admin', 'info'],
    ['member', 'Member', 'neutral'],
    ['editor', 'Editor', 'info'],
    ['viewer', 'Viewer', 'neutral'],
    // A project row opting a member out of the organization's default (F20 PR15).
    ['none', 'No access', 'warning'],
  ])('draws %s as "%s" in the %s tone', (role, label, tone) => {
    render(<RoleChip role={role} />)

    const chip = screen.getByText(label)
    expect(chip).toHaveAttribute('data-slot', 'chip')
    expect(chip).toHaveAttribute('data-tone', tone)
  })

  it('falls back to the raw role, in the neutral tone, for one it does not know', () => {
    render(<RoleChip role={'auditor' as unknown as ChipRole} />)

    expect(screen.getByText('auditor')).toHaveAttribute('data-tone', 'neutral')
  })
})
