// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ENTERPRISE_TEASERS, EDITIONS_DOCS_URL, ORG_CREATION_TEASER } from '@/extensions/teasers'
import { EnterpriseFeature } from './EnterpriseFeature'

function teaserFor(id: string) {
  const teaser = ENTERPRISE_TEASERS.find((entry) => entry.item.id === id)
  if (!teaser) throw new Error(`the ${id} teaser is missing`)
  return teaser
}

describe('EnterpriseFeature', () => {
  it('says the feature is Enterprise, what it does, and links the editions page', () => {
    const teaser = teaserFor('org-sso')
    render(<EnterpriseFeature teaser={teaser} />)

    expect(
      screen.getByRole('heading', { name: 'Single sign-on: part of tripl Enterprise' }),
    ).toBeInTheDocument()
    expect(screen.getByText(teaser.summary)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Compare editions/ })).toHaveAttribute(
      'href',
      EDITIONS_DOCS_URL,
    )
  })

  it('reads as a sentence for a plural label too', () => {
    render(<EnterpriseFeature teaser={teaserFor('platform-orgs')} />)

    expect(
      screen.getByRole('heading', { name: 'Organizations: part of tripl Enterprise' }),
    ).toBeInTheDocument()
  })

  it('says what Community already has of the same kind, and where to set it up', () => {
    // A Community admin who searched "OIDC" lands on the organization's single
    // sign-on; instance-wide OpenID Connect sign-in is theirs already.
    render(<EnterpriseFeature teaser={teaserFor('org-sso')} />)

    expect(screen.getByText(/one OpenID Connect provider .* is in Community/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /How to set it up/ })).toHaveAttribute(
      'href',
      'https://docs.tripl.io/administer/admin-guide#instance-sign-in',
    )
  })

  it('offers no set-up link where Community has nothing of the kind', () => {
    render(<EnterpriseFeature teaser={ORG_CREATION_TEASER} />)

    expect(
      screen.getByRole('heading', { name: 'Creating more organizations: part of tripl Enterprise' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /How to set it up/ })).toBeNull()
  })
})
