// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ENTERPRISE_TEASERS, EDITIONS_DOCS_URL } from '@/extensions/teasers'
import { EnterpriseFeature } from './EnterpriseFeature'

describe('EnterpriseFeature', () => {
  it('says the feature is Enterprise, what it does, and links the editions page', () => {
    const teaser = ENTERPRISE_TEASERS.find((entry) => entry.item.id === 'org-sso')
    if (!teaser) throw new Error('the single sign-on teaser is missing')
    render(<EnterpriseFeature teaser={teaser} />)

    expect(
      screen.getByRole('heading', { name: 'Single sign-on is part of Tripl Enterprise' }),
    ).toBeInTheDocument()
    expect(screen.getByText(teaser.summary)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Compare editions/ })).toHaveAttribute(
      'href',
      EDITIONS_DOCS_URL,
    )
  })
})
