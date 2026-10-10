import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { NotFoundState } from '@/components/not-found-state'
import type { Project } from '@/types'
import { ProjectNotFound } from './project-not-found'

describe('ProjectNotFound layout', () => {
  it('keeps the 404 panel compact, so the project list sits under the way back', () => {
    // The panel's own 60vh centring, inside a screen that already centres the
    // group, left "Your projects" floating far below the button.
    const projects = [{ id: 'p1', name: 'Alpha', slug: 'alpha' }] as unknown as Project[]
    render(
      <MemoryRouter>
        <ProjectNotFound slug="gone" projects={projects} />
      </MemoryRouter>,
    )
    const panel = screen.getByRole('heading', { level: 1, name: 'Project not found' }).parentElement
    expect(panel).not.toHaveClass('min-h-[60vh]')
    expect(screen.getByRole('navigation', { name: 'Your projects' })).toBeInTheDocument()
  })

  it('leaves the page-level 404 centred in the content column', () => {
    render(
      <MemoryRouter>
        <NotFoundState />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { level: 1, name: 'Page not found' }).parentElement).toHaveClass(
      'min-h-[60vh]',
    )
  })
})
