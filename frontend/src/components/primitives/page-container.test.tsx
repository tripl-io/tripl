import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PageContainer } from './page-container'

// Detail pages padded themselves inside the shell's padding.
describe('PageContainer', () => {
  it('adds the list-page rhythm and no padding or centring of its own', () => {
    render(<PageContainer data-testid="page">Body</PageContainer>)
    const page = screen.getByTestId('page')
    expect(page).toHaveAttribute('data-width', 'full')
    expect(page.className).toContain('space-y-6')
    expect(page.className).not.toMatch(/(^|\s)(p|px|pt|mx)-/)
    expect(page.className).not.toContain('max-w-')
  })

  it('caps a narrow page at the form width', () => {
    render(
      <PageContainer data-testid="page" width="narrow" className="space-y-5">
        Body
      </PageContainer>,
    )
    const page = screen.getByTestId('page')
    expect(page).toHaveAttribute('data-width', 'narrow')
    expect(page.className).toContain('max-w-[880px]')
    // The caller's rhythm wins over the default one.
    expect(page.className).toContain('space-y-5')
    expect(page.className).not.toContain('space-y-6')
  })
})
