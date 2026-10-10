import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { InactiveGroup, ReadOnlyValue } from './ServiceSettingsPrimitives'

describe('InactiveGroup', () => {
  it('passes the rows through untouched while its switch is on', () => {
    const { container } = render(
      <InactiveGroup inactive={false}>
        <input aria-label="Model" />
      </InactiveGroup>,
    )

    expect(screen.getByLabelText('Model')).toBeEnabled()
    expect(container.querySelector('[data-inactive]')).toBeNull()
  })

  it('marks the rows inactive but leaves them editable', () => {
    const { container } = render(
      <InactiveGroup inactive>
        <input aria-label="Model" />
      </InactiveGroup>,
    )

    const group = container.querySelector('[data-inactive="true"]')
    expect(group).not.toBeNull()
    expect(group).toContainElement(screen.getByLabelText('Model'))
    // De-emphasised, not disabled: preparing a config before switching it on is valid.
    expect(screen.getByLabelText('Model')).toBeEnabled()
  })

  it('draws no caption of its own: the switch or the card says why', () => {
    const { container } = render(
      <InactiveGroup inactive>
        <input aria-label="Model" />
      </InactiveGroup>,
    )

    expect(container.querySelector('[data-inactive] p')).toBeNull()
  })
})

describe('ReadOnlyValue', () => {
  it('reports a value as text, not as an input that cannot move', () => {
    const { container } = render(<ReadOnlyValue value="https://embed.example/v1" />)

    expect(screen.getByText('https://embed.example/v1')).toHaveAttribute('title', 'https://embed.example/v1')
    expect(container.querySelector('input')).toBeNull()
  })

  it('shows a dash for an empty value', () => {
    render(<ReadOnlyValue value="" />)

    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
