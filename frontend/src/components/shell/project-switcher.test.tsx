import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { Project } from '@/types'
import { ProjectSwitcher } from './project-switcher'

function project(slug: string, name: string): Project {
  return { id: `id-${slug}`, slug, name } as Project
}

function renderSwitcher(projects: Project[], activeProject?: Project, loading = false) {
  return render(
    <MemoryRouter>
      <ProjectSwitcher
        activeProject={activeProject}
        projects={projects}
        loading={loading}
        onPick={() => {}}
        canCreateProject
      />
    </MemoryRouter>,
  )
}

describe('ProjectSwitcher subtitle', () => {
  it('says there are no projects yet instead of offering a choice among "0 projects"', () => {
    renderSwitcher([])
    expect(screen.getByText('Choose a project')).toBeInTheDocument()
    expect(screen.getByText('No projects yet')).toBeInTheDocument()
    expect(screen.queryByText('0 projects')).toBeNull()
  })

  it('counts the projects to choose from when none is open', () => {
    renderSwitcher([project('demo', 'Demo'), project('shop', 'Shop')])
    expect(screen.getByText('2 projects')).toBeInTheDocument()
  })

  it('shows the open project’s slug, not a count', () => {
    const demo = project('demo', 'Demo')
    renderSwitcher([demo, project('shop', 'Shop')], demo)
    expect(screen.getByText('Demo')).toBeInTheDocument()
    expect(screen.getByText('demo')).toBeInTheDocument()
    expect(screen.queryByText('2 projects')).toBeNull()
  })

  it('says it is loading before the list arrives', () => {
    renderSwitcher([], undefined, true)
    expect(screen.getByText('loading…')).toBeInTheDocument()
    expect(screen.queryByText('No projects yet')).toBeNull()
  })
})
