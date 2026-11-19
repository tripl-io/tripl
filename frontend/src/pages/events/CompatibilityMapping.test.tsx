import { render, screen, fireEvent } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { CompatibilityMapping, mappingError } from './CompatibilityMapping'

it('validates the JSON object and the persisted UTF-8 size', () => {
  expect(mappingError('')).toBeNull()
  expect(mappingError('{}')).toBeNull()
  for (const value of ['[1]', 'null', '{', JSON.stringify({ key: '€'.repeat(680) })]) {
    expect(mappingError(value)).toBeTruthy()
  }
})

it('offers a draft example and the command for the selected branch copy', () => {
  const onChange = vi.fn()
  render(<CompatibilityMapping id="map" value="" onChange={onChange} eventId="copy" slug="shop" branchId="branch" />)
  fireEvent.click(screen.getByRole('button', { name: 'Insert example' }))
  expect(JSON.parse(onChange.mock.calls[0]![0]).rules).toHaveLength(2)
  expect(screen.getByText(/tripl compatibility preview/)).toHaveTextContent('--branch=branch')
  expect(screen.queryByText(/tripl compatibility model/)).toBeNull()
})
