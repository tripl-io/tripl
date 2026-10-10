import { render, screen } from '@testing-library/react'
import { Eye, FlaskConical } from 'lucide-react'
import { describe, expect, it } from 'vitest'
import { ShellBanner } from './shell-banner'

/** The one frame every banner in the shell's banner slot shares. */
describe('ShellBanner', () => {
  it('is a status line by default, with its text and action, and passes the test id through', () => {
    render(
      <ShellBanner tone="warning" icon={Eye} data-testid="step-in" action={<button type="button">End now</button>}>
        Read-only step-in
      </ShellBanner>,
    )
    const banner = screen.getByTestId('step-in')
    expect(banner).toHaveAttribute('role', 'status')
    expect(banner).toHaveTextContent('Read-only step-in')
    expect(banner).toHaveStyle({ background: 'var(--warning-soft)' })
    expect(screen.getByRole('button', { name: 'End now' })).toBeInTheDocument()
    expect(banner.querySelector('svg')).toHaveClass('text-warning')
  })

  it('can stand as a note in the accent tone', () => {
    render(
      <ShellBanner tone="accent" icon={FlaskConical} role="note" data-testid="demo">
        Public demo.
      </ShellBanner>,
    )
    const banner = screen.getByTestId('demo')
    expect(banner).toHaveAttribute('role', 'note')
    expect(banner).toHaveStyle({ background: 'var(--accent-soft)' })
    expect(banner.querySelector('svg')).toHaveClass('text-accent')
    expect(banner.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
  })
})
