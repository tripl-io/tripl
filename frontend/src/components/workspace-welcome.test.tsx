import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DEMO_PROVISION_ESTIMATE } from '@/demo/provisioningPhases'
import { CONCEPTS_DOCS_URL } from '@/lib/docsSite'
import { WorkspaceWelcome } from './workspace-welcome'
import { WELCOME_PILLARS } from './workspace-welcome-pillars'

type WelcomeProps = Parameters<typeof WorkspaceWelcome>[0]

function renderWelcome(overrides: Partial<WelcomeProps> = {}) {
  const props: WelcomeProps = {
    memberSeesOnlyAddedProjects: false,
    isProvisioningDemo: false,
    onGenerateDemo: vi.fn(),
    onCreateProject: vi.fn(),
    ...overrides,
  }
  render(<WorkspaceWelcome {...props} />)
  return props
}

describe('WorkspaceWelcome', () => {
  it('renders the hero copy with both CTAs for a role that can create projects', () => {
    const props = renderWelcome()

    expect(screen.getByText('Tracking plan operations')).toBeInTheDocument()
    expect(screen.getByText('Keep your product analytics honest')).toBeInTheDocument()
    expect(
      screen.getByText(/single place where your team writes down what you/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/No new SDK to ship and nothing to re-instrument/),
    ).toBeInTheDocument()

    // Primary demo CTA with its caption, secondary empty-project CTA with its own.
    // The wait-time claim comes from the one shared constant the provisioning
    // dialog also renders, so the hero cannot drift from it.
    expect(
      screen.getByText(new RegExp(`Builds a complete example in ${DEMO_PROVISION_ESTIMATE}`)),
    ).toBeInTheDocument()
    // The create path mentions templates (F21): the same dialog offers them.
    expect(
      screen.getByText(
        'Start empty or from an industry template, then connect your own warehouse.',
      ),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Generate demo project/i }))
    expect(props.onGenerateDemo).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /New project/i }))
    expect(props.onCreateProject).toHaveBeenCalledTimes(1)
  })

  it('disables the demo CTA and swaps its label while provisioning', () => {
    const props = renderWelcome({ isProvisioningDemo: true })

    const generating = screen.getByRole('button', { name: /Generating…/ })
    expect(generating).toBeDisabled()
    expect(
      screen.queryByRole('button', { name: /Generate demo project/i }),
    ).not.toBeInTheDocument()

    fireEvent.click(generating)
    expect(props.onGenerateDemo).not.toHaveBeenCalled()
  })

  it('renders every product pillar as a card', () => {
    renderWelcome()

    for (const pillar of WELCOME_PILLARS) {
      expect(screen.getByText(pillar.eyebrow)).toBeInTheDocument()
      expect(screen.getByText(pillar.title)).toBeInTheDocument()
      expect(screen.getByText(pillar.description)).toBeInTheDocument()
    }
  })

  it("tells a member the team's projects may exist, and keeps both ways in", () => {
    renderWelcome({ memberSeesOnlyAddedProjects: true })

    expect(screen.getByText(/You see a project here once you are added to it/)).toHaveTextContent(
      'ask an organization owner or admin to add you under Settings › Project › Access',
    )
    // Members may create projects of their own (F20 PR4): the note does not
    // take the buttons away.
    expect(screen.getByRole('button', { name: /Generate demo project/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New project/i })).toBeInTheDocument()
  })

  it('says nothing about access to someone who sees every project', () => {
    renderWelcome({ memberSeesOnlyAddedProjects: false })

    expect(screen.queryByText(/You see a project here once you are added to it/)).toBeNull()
  })

  it('links to the concepts docs in a new tab from the closing strip', () => {
    renderWelcome()

    expect(
      screen.getByText(/What a project does on a schedule: scan the warehouse/),
    ).toBeInTheDocument()
    // The reader has no project yet, so the strip does not talk about one.
    expect(screen.queryByText(/your real project/)).toBeNull()

    const link = screen.getByRole('link', { name: /Read the concepts/ })
    // The concepts page itself, not the docs home.
    expect(link).toHaveAttribute('href', CONCEPTS_DOCS_URL)
    expect(CONCEPTS_DOCS_URL).toBe('https://docs.tripl.io/use/concepts')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer')
    // a link that leaves the app says so in its name.
    expect(link).toHaveAccessibleName('Read the concepts (opens in a new tab)')
  })
})
