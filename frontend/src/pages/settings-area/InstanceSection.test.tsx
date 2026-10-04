import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import InstanceSection from './InstanceSection'

function InstanceRoute() {
  const { section = '' } = useParams<{ section: string }>()
  return <InstanceSection section={section} />
}

describe('InstanceSection', () => {
  // an unknown section rendered Runtime under a URL that said otherwise.
  it('redirects an unknown section to Runtime instead of rendering it under the wrong URL', async () => {
    render(
      <MemoryRouter initialEntries={['/settings/instance/typo']}>
        <Routes>
          <Route path="/settings/instance/runtime" element={<p>Runtime route</p>} />
          <Route path="/settings/instance/:section" element={<InstanceRoute />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByText('Runtime route')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Runtime' })).toBeNull()
  })
})
